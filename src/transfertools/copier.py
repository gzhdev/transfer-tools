"""分块写入式安全复制核心（design.md D1~D4）。

设计约束：复制路径上只允许出现普通文件 I/O —— ``open`` / ``read`` / ``write`` /
``flush`` / ``os.fsync``。禁止使用任何直接复制 API（``shutil.copy``、
``shutil.copy2``、``shutil.copyfile``、``shutil.copyfileobj``、``os.sendfile``、
``os.copy_file_range``、平台等价的 ``CopyFile`` 原语等），因为安全软件的行为审计
规则主要 hook 这些复制语义的调用。``tests/test_no_copy_api.py`` 会对本包的源码做
静态断言，防止回归。

流程：创建/截断目标文件 → （可选）预分配 → 分块顺序写入 → flush + fsync →
双端 SHA-256 比对 → 保留权限/时间戳；任一环节失败都会尽力删除目标上的半成品，
不留未校验的残文件。

GUI 复用钩子（design.md D-g3，全部为可选关键字参数，默认值下行为与既有 CLI 路径
完全一致）：``copy_file(..., on_bytes=..., cancel_event=...)`` 提供字节级进度回调与
协作式取消；``cancel_event`` 置位后在块边界抛出 :class:`CopyCancelledError`，
走上面同一条半成品清理路径。
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
import threading
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

__all__ = [
    "MIB",
    "DEFAULT_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "MAX_CHUNK_SIZE",
    "STAGE_WRITE",
    "STAGE_VERIFY",
    "STAGE_DONE",
    "SafeCopyError",
    "InvalidChunkSizeError",
    "SourceError",
    "DestinationError",
    "SameFileError",
    "VerificationError",
    "CopyCancelledError",
    "CopyResult",
    "validate_chunk_size",
    "iter_chunks",
    "sha256_file",
    "verify_digests",
    "resolve_destination",
    "canonical_path",
    "is_same_file",
    "is_within",
    "copy_file",
]

MIB = 1024 * 1024

#: 默认块大小 4 MiB（design D2：落在 1~8 MiB 建议区间内）。
DEFAULT_CHUNK_SIZE = 4 * MIB
#: 块大小下界 1 MiB。
MIN_CHUNK_SIZE = 1 * MIB
#: 块大小上界 8 MiB。
MAX_CHUNK_SIZE = 8 * MIB

#: 进度阶段：开始写入。
STAGE_WRITE = "write"
#: 进度阶段：开始校验。
STAGE_VERIFY = "verify"
#: 进度阶段：复制完成。
STAGE_DONE = "done"

#: 仅警告不中断的回调（如预分配失败、元数据保留失败）。
WarnFn = Callable[[str], None]
#: 进度回调，参数为 STAGE_* 常量之一。
ProgressFn = Callable[[str], None]
#: 字节进度回调（design D-g3）：参数为 ``(已写入字节数, 该文件总字节数)``。
BytesProgressFn = Callable[[int, int], None]


class SafeCopyError(Exception):
    """本模块抛出的所有错误基类。"""


class InvalidChunkSizeError(SafeCopyError, ValueError):
    """块大小不在允许区间内（design D2：1~8 MiB）。"""


class SourceError(SafeCopyError):
    """源路径不可用（不存在、是目录、不是普通文件）。"""


class DestinationError(SafeCopyError):
    """目标路径不可用（例如目标是一个目录）。"""


class SameFileError(SafeCopyError):
    """源与目标指向同一个文件（或目标落在源目录内部），拒绝复制以免截断源数据。"""


class VerificationError(SafeCopyError):
    """SHA-256 双端比对不一致（design D3、D4 专用异常）。"""


class CopyCancelledError(SafeCopyError):
    """协作式取消被触发（design D-g3）：GUI 置位 ``cancel_event`` 后于块边界抛出。

    与其它 :class:`SafeCopyError` 一样，目标半成品会走既有清理路径删除。
    """


@dataclass(frozen=True)
class CopyResult:
    """一次成功复制的元信息。"""

    src: Path
    dst: Path
    size: int
    #: 校验通过的目标端（亦即源端）SHA-256 十六进制摘要。
    sha256: str
    chunk_size: int
    #: 是否成功完成了预分配（预分配被禁用或降级时为 False）。
    preallocated: bool
    #: 是否成功保留了权限与时间戳。
    metadata_preserved: bool


def _default_warn(message: str) -> None:
    warnings.warn(message, RuntimeWarning, stacklevel=3)


def _check_cancelled(
    cancel_event: threading.Event | None, path: str | os.PathLike[str]
) -> None:
    """协作式取消检查点（design D-g3）。

    仅在 ``cancel_event`` 已置位时抛出 :class:`CopyCancelledError`；传 ``None``
    （CLI 与既有调用点）时不做任何事，行为与变更前逐字节一致。
    """
    if cancel_event is not None and cancel_event.is_set():
        raise CopyCancelledError(
            f"复制已取消: {path}（目标上的半成品将被删除）"
        )


def validate_chunk_size(chunk_size: int) -> int:
    """校验块大小落在 1~8 MiB（含端点），返回原值。

    :raises InvalidChunkSizeError: 不是正整数或超出区间。
    """
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise InvalidChunkSizeError(
            f"块大小必须是整数字节数，收到 {chunk_size!r}"
        )
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise InvalidChunkSizeError(
            f"块大小必须在 {MIN_CHUNK_SIZE // MIB}~{MAX_CHUNK_SIZE // MIB} MiB 之间，"
            f"收到 {chunk_size} 字节（{chunk_size / MIB:.3f} MiB）"
        )
    return chunk_size


def iter_chunks(stream: IO[bytes], chunk_size: int) -> Iterator[bytes]:
    """按 `chunk_size` 顺序产出 `stream` 的数据块。

    只使用普通 ``read``，不涉及任何复制 API。
    """
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        yield chunk


def sha256_file(
    path: str | os.PathLike[str],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    *,
    cancel_event: threading.Event | None = None,
) -> str:
    """分块计算 `path` 的 SHA-256 十六进制摘要。

    :param cancel_event: 可选协作式取消事件（design D-g3）；置位时在块边界抛出
        :class:`CopyCancelledError`。传 ``None`` 时行为与变更前一致。
    """
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter_chunks(stream, chunk_size):
            _check_cancelled(cancel_event, path)
            digest.update(chunk)
    return digest.hexdigest()


def verify_digests(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str]:
    """分别重新打开源与目标文件计算摘要，返回 ``(src_digest, dst_digest)``。

    design D3：在 fsync 之后重新读回双方内容计算，而不是写入时顺带累计，
    避免源文件在写入期间被第三方修改导致的误判。

    :param cancel_event: 可选协作式取消事件（design D-g3），校验读回期间按块检查。
    """
    src_digest = sha256_file(src, chunk_size, cancel_event=cancel_event)
    dst_digest = sha256_file(dst, chunk_size, cancel_event=cancel_event)
    return src_digest, dst_digest


def resolve_destination(src: Path, dst: Path) -> Path:
    """目标为已存在目录时返回 ``<dir>/<源文件名>``，否则原样返回 `dst`。"""
    if dst.is_dir():
        return dst / src.name
    return dst


def canonical_path(path: str | os.PathLike[str]) -> str:
    """返回规范化后的绝对路径：解析符号链接、折叠 ``.`` 与 ``..``。

    目标尚不存在时同样可用（``..`` 按词法折叠），因此能在「目标还没被创建」的阶段
    用于同源判断。
    """
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def is_same_file(
    src: str | os.PathLike[str], dst: str | os.PathLike[str]
) -> bool:
    """判断 `src` 与 `dst` 是否指向同一个文件。

    - 两个路径都存在时用 :func:`os.path.samefile` 按 ``(st_dev, st_ino)`` 判定，
      因此硬链接、符号链接、大小写不敏感文件系统都能正确识别；
    - 目标不存在（或无法 stat）时退化为规范化绝对路径比较，
      拦截 ``./f`` 与 ``f``、``dir/../f`` 与 ``f`` 这类写法不同但指向同一位置的输入。
    """
    try:
        return os.path.samefile(src, dst)
    except OSError:
        pass
    return canonical_path(src) == canonical_path(dst)


def is_within(
    path: str | os.PathLike[str], parent: str | os.PathLike[str]
) -> bool:
    """`path` 是否等于 `parent` 或位于 `parent` 目录树内部（按规范化绝对路径判断）。"""
    return Path(canonical_path(path)).is_relative_to(canonical_path(parent))


def copy_file(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    preallocate: bool = True,
    preserve_metadata: bool = True,
    warn: WarnFn | None = None,
    progress: ProgressFn | None = None,
    on_bytes: BytesProgressFn | None = None,
    cancel_event: threading.Event | None = None,
) -> CopyResult:
    """以「创建 + 分块写入 + fsync + SHA-256 校验」方式把 `src` 复制到 `dst`。

    :param chunk_size: 块大小（字节），必须落在 1~8 MiB。
    :param preallocate: 是否尽力预分配目标文件；不支持时降级并仅警告。
    :param preserve_metadata: 是否在校验通过后保留源文件的权限与时间戳；
        平台/文件系统不支持时降级并仅警告。
    :param warn: 警告回调，默认走 :func:`warnings.warn`；CLI 会传入 stderr 打印器。
    :param progress: 进度回调，收到 :data:`STAGE_WRITE` / :data:`STAGE_VERIFY` /
        :data:`STAGE_DONE`。
    :param on_bytes: 可选字节进度回调（design D-g3，GUI 使用）：每写完一块调用一次，
        参数为 ``(已写入字节数, 源文件总字节数)``。默认 ``None``，此时不做任何额外工作。
    :param cancel_event: 可选协作式取消事件（design D-g3，GUI 使用）：写入循环与校验
        读回循环每次迭代检查，置位时抛出 :class:`CopyCancelledError`，目标半成品随后由
        下面既有的清理路径删除。默认 ``None``（CLI 与既有调用点）时行为完全不变。
    :raises SourceError: 源不存在、是目录或不是普通文件。
    :raises DestinationError: 目标路径本身是目录。
    :raises SameFileError: 源与目标指向同一个文件（同源防护，见 :func:`is_same_file`）；
        该检查在任何写入之前完成，源文件保持零改动。
    :raises CopyCancelledError: 协作式取消被触发（``cancel_event`` 置位）。
    :raises VerificationError: 双端摘要不一致（目标半成品已被删除）。
    :raises OSError: 写入 / fsync 失败（目标半成品已被尽力删除）。
    """
    chunk_size = validate_chunk_size(chunk_size)
    warn = warn or _default_warn
    progress = progress or (lambda _stage: None)

    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.exists():
        raise SourceError(f"源路径不存在: {src_path}")
    if src_path.is_dir():
        raise SourceError(f"源是目录，单文件复制只接受普通文件: {src_path}")
    if not src_path.is_file():
        raise SourceError(f"源不是普通文件: {src_path}")
    if dst_path.is_dir():
        raise DestinationError(f"目标路径已存在且是目录: {dst_path}")

    # 同源防护：必须在 open(dst, 'wb') 之前判定。否则目标就是源文件本身时，
    # 'wb' 会先把源截断，预分配补零后读到全零、双端哈希还「一致」，静默丢数据。
    if is_same_file(src_path, dst_path):
        raise SameFileError(
            f"源与目标是同一个文件，拒绝复制（会截断源文件本身）: "
            f"{src_path} 与 {dst_path} 指向同一文件"
        )

    src_stat = src_path.stat()

    dst_created = False
    written = 0
    preallocated = False
    try:
        # D1：先在目标路径「创建」文件（'wb' 会截断已存在的同名文件），
        # 之后只做普通 read/write，不触碰任何复制 API。
        dst_stream = open(dst_path, "wb")
        dst_created = True
        try:
            progress(STAGE_WRITE)
            if preallocate:
                preallocated = _try_preallocate(dst_stream, src_stat.st_size, warn)
            with open(src_path, "rb") as src_stream:
                for chunk in iter_chunks(src_stream, chunk_size):
                    # D-g3：块边界检查取消；置位即抛 CopyCancelledError，
                    # 由下面的 except BaseException 删除半成品。
                    _check_cancelled(cancel_event, src_path)
                    dst_stream.write(chunk)
                    written += len(chunk)
                    if on_bytes is not None:
                        on_bytes(written, src_stat.st_size)
            # 写入阶段结束、fsync 前再检查一次，避免大文件 fsync 期间取消无反馈。
            _check_cancelled(cancel_event, src_path)
            dst_stream.flush()
            os.fsync(dst_stream.fileno())
        finally:
            dst_stream.close()

        # D3：fsync 之后重新打开双方文件计算 SHA-256 并比对。
        progress(STAGE_VERIFY)
        src_digest, dst_digest = verify_digests(
            src_path, dst_path, chunk_size, cancel_event=cancel_event
        )
        if src_digest != dst_digest:
            raise VerificationError(
                f"完整性校验失败: {src_path} 与 {dst_path} 的 SHA-256 不一致"
                f"（源 {src_digest} / 目标 {dst_digest}）。"
                "目标文件已删除；若源文件在复制过程中被修改，请重试。"
            )

        metadata_preserved = False
        if preserve_metadata:
            metadata_preserved = _preserve_metadata(src_path, dst_path, warn)

        progress(STAGE_DONE)
        return CopyResult(
            src=src_path,
            dst=dst_path,
            size=written,
            sha256=dst_digest,
            chunk_size=chunk_size,
            preallocated=preallocated,
            metadata_preserved=metadata_preserved,
        )
    except BaseException:
        # D4：写入 / fsync / 校验任一失败都尽力删除半成品（含 Ctrl-C 中断），
        # 不留未校验的残文件；删除失败仅警告，不掩盖原始错误。
        if dst_created:
            _remove_partial(dst_path, warn)
        raise


def _try_preallocate(dst_stream: IO[bytes], size: int, warn: WarnFn) -> bool:
    """尽力预分配 `size` 字节（design D2/风险项：不支持时降级为普通创建）。"""
    if size <= 0:
        return False
    posix_fallocate: Any = getattr(os, "posix_fallocate", None)
    if posix_fallocate is None:
        warn(
            "当前平台不支持预分配（os.posix_fallocate 不可用），"
            "已降级为普通截断创建。"
        )
        return False
    fd = dst_stream.fileno()
    try:
        posix_fallocate(fd, 0, size)
    except OSError as exc:
        # 某些文件系统（如外部存储上的 FAT32/exFAT）不支持 fallocate；
        # 部分实现失败时可能已写入数据，因此复位文件长度与偏移。
        try:
            os.ftruncate(fd, 0)
            dst_stream.seek(0)
        except OSError:  # pragma: no cover - 复位失败也会被后续写入/校验暴露
            pass
        warn(f"预分配 {size} 字节失败（{exc}），已降级为普通截断创建。")
        return False
    return True


def _preserve_metadata(src: Path, dst: Path, warn: WarnFn) -> bool:
    """保留源文件的权限位与时间戳；失败仅警告（external storage 常见不支持）。"""
    try:
        src_stat = src.stat()
    except OSError as exc:  # pragma: no cover - 复制刚成功，源通常仍可 stat
        warn(f"读取源文件元数据失败，跳过权限/时间戳保留: {exc}")
        return False

    preserved = True
    try:
        os.chmod(dst, stat_module.S_IMODE(src_stat.st_mode))
    except OSError as exc:
        preserved = False
        warn(f"保留文件权限失败（{dst}）: {exc}")
    try:
        os.utime(dst, (src_stat.st_atime, src_stat.st_mtime))
    except OSError as exc:
        preserved = False
        warn(f"保留文件时间戳失败（{dst}）: {exc}")
    return preserved


def _remove_partial(dst: Path, warn: WarnFn) -> None:
    """删除半成品目标文件；删除失败仅警告（design D4）。"""
    try:
        os.remove(dst)
    except FileNotFoundError:
        pass
    except OSError as exc:
        warn(f"清理半成品目标文件失败，请手动删除 {dst}: {exc}")
