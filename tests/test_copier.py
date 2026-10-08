"""``transfertools.copier`` 的单元测试：分块边界、校验、失败清理、预分配、元数据。"""

from __future__ import annotations

import hashlib
import io
import os
import random
import stat
import warnings
from pathlib import Path

import pytest

from transfertools import copier

MIB = copier.MIB


def make_data(size: int, seed: int = 20261008) -> bytes:
    """生成确定性随机数据。"""
    return random.Random(seed).randbytes(size)


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def denied_remove(path: object) -> None:
    """模拟删除半成品失败（只读/被占用）。"""
    raise OSError(13, "Permission denied")


class RecordingReader:
    """代理文件对象，记录每次 ``read`` 请求的块大小（用于验证分块粒度）。"""

    def __init__(self, handle: object) -> None:
        self._handle = handle
        self.sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.sizes.append(size)
        return self._handle.read(size)  # type: ignore[attr-defined]

    def __enter__(self) -> "RecordingReader":
        return self

    def __exit__(self, *exc_info: object) -> object:
        return self._handle.__exit__(*exc_info)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# 常量与参数校验（design D2）
# --------------------------------------------------------------------------- #


def test_chunk_size_defaults_follow_design() -> None:
    assert copier.DEFAULT_CHUNK_SIZE == 4 * MIB
    assert copier.MIN_CHUNK_SIZE == 1 * MIB
    assert copier.MAX_CHUNK_SIZE == 8 * MIB
    assert copier.MIN_CHUNK_SIZE <= copier.DEFAULT_CHUNK_SIZE <= copier.MAX_CHUNK_SIZE


@pytest.mark.parametrize("chunk_size", [MIB, 4 * MIB, copier.MAX_CHUNK_SIZE])
def test_validate_chunk_size_accepts_range(chunk_size: int) -> None:
    assert copier.validate_chunk_size(chunk_size) == chunk_size


@pytest.mark.parametrize(
    "chunk_size",
    [0, -1, MIB - 1, copier.MAX_CHUNK_SIZE + 1, 8 * MIB + 1, 1.5, "4194304", None, True],
)
def test_validate_chunk_size_rejects_out_of_range(chunk_size: object) -> None:
    with pytest.raises(copier.InvalidChunkSizeError):
        copier.validate_chunk_size(chunk_size)  # type: ignore[arg-type]


def test_copy_file_rejects_out_of_range_chunk_size(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", b"x")
    with pytest.raises(copier.InvalidChunkSizeError):
        copier.copy_file(src, tmp_path / "dst.bin", chunk_size=123)
    assert not (tmp_path / "dst.bin").exists()


# --------------------------------------------------------------------------- #
# 分块读取与哈希工具
# --------------------------------------------------------------------------- #


def test_iter_chunks_splits_at_chunk_size() -> None:
    assert list(copier.iter_chunks(io.BytesIO(b"0123456789"), 3)) == [b"012", b"345", b"678", b"9"]


def test_iter_chunks_single_chunk_when_smaller_than_chunk_size() -> None:
    assert list(copier.iter_chunks(io.BytesIO(b"ab"), 4096)) == [b"ab"]


def test_iter_chunks_returns_nothing_for_empty_stream() -> None:
    assert list(copier.iter_chunks(io.BytesIO(b""), 4096)) == []


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    data = make_data(3 * MIB + 7)
    src = write_file(tmp_path / "src.bin", data)
    assert copier.sha256_file(src) == sha256_of(data)
    assert copier.sha256_file(src, chunk_size=MIB) == sha256_of(data)


def test_verify_digests_returns_both_sides(tmp_path: Path) -> None:
    data = make_data(128)
    src = write_file(tmp_path / "src.bin", data)
    dst = write_file(tmp_path / "dst.bin", data)
    assert copier.verify_digests(src, dst) == (sha256_of(data), sha256_of(data))


def test_verify_digests_detects_tampered_destination(tmp_path: Path) -> None:
    data = make_data(64)
    src = write_file(tmp_path / "src.bin", data)
    dst = write_file(tmp_path / "dst.bin", data)
    with open(dst, "ab") as handle:
        handle.write(b"tampered")
    src_digest, dst_digest = copier.verify_digests(src, dst)
    assert src_digest != dst_digest


# --------------------------------------------------------------------------- #
# 目标路径解析
# --------------------------------------------------------------------------- #


def test_resolve_destination_uses_directory(tmp_path: Path) -> None:
    src = write_file(tmp_path / "a.txt", b"a")
    target_dir = tmp_path / "out"
    target_dir.mkdir()
    assert copier.resolve_destination(src, target_dir) == target_dir / "a.txt"


def test_resolve_destination_keeps_file_path(tmp_path: Path) -> None:
    src = write_file(tmp_path / "a.txt", b"a")
    explicit = tmp_path / "renamed.txt"
    assert copier.resolve_destination(src, explicit) == explicit


# --------------------------------------------------------------------------- #
# 分块写入式复制：边界与内容一致性（需求「分块写入式文件复制」）
# --------------------------------------------------------------------------- #


def test_copy_small_file_roundtrip(tmp_path: Path) -> None:
    data = make_data(64)
    src = write_file(tmp_path / "src.bin", data)
    dst = tmp_path / "dst.bin"

    result = copier.copy_file(src, dst)

    assert dst.read_bytes() == data
    assert result.size == len(data)
    assert result.sha256 == sha256_of(data)
    assert result.chunk_size == copier.DEFAULT_CHUNK_SIZE
    assert result.src == src and result.dst == dst


@pytest.mark.parametrize("size", [0, 1, MIB - 1, MIB, MIB + 1, 2 * MIB + 4096])
def test_copy_chunk_boundaries(tmp_path: Path, size: int) -> None:
    """块大小恰好整除 / 差一字节 / 多块的边界都必须字节一致。"""
    data = make_data(size)
    src = write_file(tmp_path / "src.bin", data)
    dst = tmp_path / "dst.bin"

    result = copier.copy_file(src, dst, chunk_size=MIB)

    assert dst.read_bytes() == data
    assert result.size == size
    assert result.sha256 == sha256_of(data)


def test_copy_with_max_chunk_size(tmp_path: Path) -> None:
    data = make_data(2 * copier.MAX_CHUNK_SIZE + 3)
    src = write_file(tmp_path / "src.bin", data)
    dst = tmp_path / "dst.bin"

    copier.copy_file(src, dst, chunk_size=copier.MAX_CHUNK_SIZE)

    assert dst.stat().st_size == len(data)
    assert copier.sha256_file(dst, copier.MAX_CHUNK_SIZE) == sha256_of(data)


def test_copy_overwrites_existing_destination(tmp_path: Path) -> None:
    """目标已存在且未指定保留选项 → 截断覆盖（spec 场景「目标文件已存在」）。"""
    src = write_file(tmp_path / "src.bin", b"short")
    dst = write_file(tmp_path / "dst.bin", b"a much much longer old content")

    copier.copy_file(src, dst)

    assert dst.read_bytes() == b"short"


def test_copy_leaves_no_extra_files(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(1024))
    copier.copy_file(src, tmp_path / "dst.bin")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dst.bin", "src.bin"]


def test_copy_does_not_use_direct_copy_apis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行时证明：复制期间没有调用 os.sendfile / os.copy_file_range。"""
    called: list[str] = []

    def spy(name: str):
        def _spy(*args: object, **kwargs: object) -> None:
            called.append(name)
            raise AssertionError(f"复制过程中调用了被禁用的复制 API: {name}")

        return _spy

    for name in ("sendfile", "copy_file_range", "posix_fadvise"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, spy(name), raising=False)

    data = make_data(2048)
    src = write_file(tmp_path / "src.bin", data)
    copier.copy_file(src, tmp_path / "dst.bin")

    assert called == []


# --------------------------------------------------------------------------- #
# 源 / 目标错误
# --------------------------------------------------------------------------- #


def test_missing_source_raises_and_creates_nothing(tmp_path: Path) -> None:
    dst = tmp_path / "dst.bin"
    with pytest.raises(copier.SourceError, match="不存在"):
        copier.copy_file(tmp_path / "missing.bin", dst)
    assert not dst.exists()


def test_missing_source_keeps_preexisting_destination(tmp_path: Path) -> None:
    """源不可用时不应误删已存在的目标文件。"""
    dst = write_file(tmp_path / "dst.bin", b"keep me")
    with pytest.raises(copier.SourceError):
        copier.copy_file(tmp_path / "missing.bin", dst)
    assert dst.read_bytes() == b"keep me"


def test_directory_source_raises(tmp_path: Path) -> None:
    src_dir = tmp_path / "srcdir"
    src_dir.mkdir()
    with pytest.raises(copier.SourceError, match="目录"):
        copier.copy_file(src_dir, tmp_path / "dst.bin")


def test_directory_destination_raises(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", b"x")
    dst_dir = tmp_path / "dstdir"
    dst_dir.mkdir()
    with pytest.raises(copier.DestinationError, match="目录"):
        copier.copy_file(src, dst_dir)


# --------------------------------------------------------------------------- #
# 完整性校验失败与失败清理（design D3/D4）
# --------------------------------------------------------------------------- #


def test_verification_failure_raises_specific_error_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要不一致 → 专用异常 + 删除半成品（spec 场景「校验不一致」）。"""
    src = write_file(tmp_path / "src.bin", make_data(4096))
    dst = tmp_path / "dst.bin"

    monkeypatch.setattr(copier, "verify_digests", lambda *a, **k: ("0" * 64, "1" * 64))

    with pytest.raises(copier.VerificationError) as excinfo:
        copier.copy_file(src, dst)

    assert not dst.exists(), "校验失败后必须删除目标上的半成品文件"
    assert "SHA-256" in str(excinfo.value)


def test_verification_failure_removes_preexisting_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = write_file(tmp_path / "src.bin", make_data(512))
    dst = write_file(tmp_path / "dst.bin", b"old")

    monkeypatch.setattr(copier, "verify_digests", lambda *a, **k: ("0" * 64, "1" * 64))

    with pytest.raises(copier.VerificationError):
        copier.copy_file(src, dst)
    assert not dst.exists()


def test_write_failure_cleans_up_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync 抛 I/O 错误（如外部存储被拔出）→ 非零退出路径 + 删除半成品。"""
    src = write_file(tmp_path / "src.bin", make_data(8192))
    dst = tmp_path / "dst.bin"

    def broken_fsync(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", broken_fsync)

    with pytest.raises(OSError) as excinfo:
        copier.copy_file(src, dst)

    assert excinfo.value.errno == 5
    assert not dst.exists(), "写入/fsync 失败后不得残留未校验文件"


def test_read_failure_cleans_up_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = write_file(tmp_path / "src.bin", make_data(8192))
    dst = tmp_path / "dst.bin"

    def broken_iter_chunks(stream: object, chunk_size: int):
        yield b"partial"
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(copier, "iter_chunks", broken_iter_chunks)

    with pytest.raises(OSError):
        copier.copy_file(src, dst)
    assert not dst.exists()


def test_cleanup_failure_only_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """删除半成品失败时只警告，不掩盖原始错误（design D4）。"""
    src = write_file(tmp_path / "src.bin", make_data(256))
    dst = tmp_path / "dst.bin"
    warnings_seen: list[str] = []

    monkeypatch.setattr(copier, "verify_digests", lambda *a, **k: ("0" * 64, "1" * 64))
    monkeypatch.setattr(os, "remove", denied_remove)

    with pytest.raises(copier.VerificationError):
        copier.copy_file(src, dst, warn=warnings_seen.append)

    assert any("清理半成品" in message for message in warnings_seen)


def test_keyboard_interrupt_also_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = write_file(tmp_path / "src.bin", make_data(4096))
    dst = tmp_path / "dst.bin"

    def interrupt(stream: object, chunk_size: int):
        yield b"partial"
        raise KeyboardInterrupt

    monkeypatch.setattr(copier, "iter_chunks", interrupt)

    with pytest.raises(KeyboardInterrupt):
        copier.copy_file(src, dst)
    assert not dst.exists()


# --------------------------------------------------------------------------- #
# 预分配（design D2 / 风险项：不支持时降级）
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not hasattr(os, "posix_fallocate"), reason="平台不支持 posix_fallocate")
def test_preallocation_is_used_when_supported(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(128 * 1024))
    dst = tmp_path / "dst.bin"

    result = copier.copy_file(src, dst)

    assert result.preallocated is True
    assert dst.read_bytes() == src.read_bytes()


def test_preallocation_disabled(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(1024))
    dst = tmp_path / "dst.bin"
    messages: list[str] = []

    result = copier.copy_file(src, dst, preallocate=False, warn=messages.append)

    assert result.preallocated is False
    assert messages == []
    assert dst.read_bytes() == src.read_bytes()


def test_preallocation_failure_degrades_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟 FAT32 等不支持 fallocate 的文件系统：降级为普通创建并仅警告。"""
    src = write_file(tmp_path / "src.bin", make_data(64 * 1024))
    dst = tmp_path / "dst.bin"
    messages: list[str] = []

    def unsupported(fd: int, offset: int, length: int) -> None:
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(os, "posix_fallocate", unsupported, raising=False)

    result = copier.copy_file(src, dst, warn=messages.append)

    assert result.preallocated is False
    assert dst.read_bytes() == src.read_bytes()
    assert any("预分配" in message for message in messages)


def test_preallocation_failure_warns_through_warnings_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = write_file(tmp_path / "src.bin", make_data(4096))
    dst = tmp_path / "dst.bin"

    def unsupported(fd: int, offset: int, length: int) -> None:
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(os, "posix_fallocate", unsupported, raising=False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        copier.copy_file(src, dst)

    assert any("预分配" in str(item.message) for item in caught)
    assert dst.read_bytes() == src.read_bytes()


def test_preallocation_skipped_for_empty_file(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", b"")
    dst = tmp_path / "dst.bin"
    messages: list[str] = []

    result = copier.copy_file(src, dst, warn=messages.append)

    assert result.preallocated is False
    assert messages == []
    assert dst.exists() and dst.stat().st_size == 0


# --------------------------------------------------------------------------- #
# 权限 / 时间戳保留
# --------------------------------------------------------------------------- #


def test_metadata_is_preserved(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(2048))
    os.chmod(src, 0o640)
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dst = tmp_path / "dst.bin"

    result = copier.copy_file(src, dst)

    assert result.metadata_preserved is True
    assert stat.S_IMODE(dst.stat().st_mode) == 0o640
    assert int(dst.stat().st_mtime) == 1_600_000_000


def test_metadata_preservation_can_be_disabled(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(64))
    os.chmod(src, 0o600)
    dst = tmp_path / "dst.bin"
    messages: list[str] = []

    result = copier.copy_file(src, dst, preserve_metadata=False, warn=messages.append)

    assert result.metadata_preserved is False
    assert messages == []


def test_metadata_failure_degrades_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标文件系统不支持 chmod/utime 时只警告，复制本身仍成功。"""
    src = write_file(tmp_path / "src.bin", make_data(1024))
    dst = tmp_path / "dst.bin"
    messages: list[str] = []

    def denied(path: object, mode: int) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", denied)

    result = copier.copy_file(src, dst, warn=messages.append)

    assert result.metadata_preserved is False
    assert dst.read_bytes() == src.read_bytes()
    assert any("权限" in message for message in messages)


# --------------------------------------------------------------------------- #
# 进度回调
# --------------------------------------------------------------------------- #


def test_progress_reports_expected_stages(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(1024 + 5))
    dst = tmp_path / "dst.bin"
    stages: list[str] = []

    copier.copy_file(src, dst, chunk_size=MIB, progress=stages.append)

    assert stages == [copier.STAGE_WRITE, copier.STAGE_VERIFY, copier.STAGE_DONE]


# --------------------------------------------------------------------------- #
# 行为级断言：分块粒度与 fsync 顺序
# --------------------------------------------------------------------------- #


def test_copy_reads_in_configured_chunk_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """实际读取粒度必须等于配置的块大小，而不是一次性读入内存。"""
    readers: list[RecordingReader] = []
    real_open = open

    def patched_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        handle = real_open(file, mode, *args, **kwargs)
        if "r" in mode and "b" in mode:
            wrapper = RecordingReader(handle)
            readers.append(wrapper)
            return wrapper
        return handle

    # raising=False：在 copier 模块命名空间里临时遮蔽内建 open，只影响被测模块。
    monkeypatch.setattr(copier, "open", patched_open, raising=False)

    data = make_data(2 * MIB + 4096)
    src = write_file(tmp_path / "src.bin", data)
    dst = tmp_path / "dst.bin"

    copier.copy_file(src, dst, chunk_size=MIB)

    assert len(readers) == 3, "写入阶段读源一次，校验阶段读源、读目标各一次"
    requested = {size for reader in readers for size in reader.sizes}
    assert requested == {MIB}, f"读取请求的块大小必须是 {MIB}，实际: {requested}"
    assert sum(len(reader.sizes) for reader in readers) >= 3  # 多块文件至少 3 次读取
    assert dst.read_bytes() == data


def test_fsync_happens_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """design D3：顺序必须是写 → flush+fsync → 再双端哈希比对。"""
    order: list[str] = []
    real_fsync = os.fsync
    real_verify = copier.verify_digests

    def spying_fsync(fd: int) -> None:
        order.append("fsync")
        real_fsync(fd)

    def spying_verify(
        src_path: Path,
        dst_path: Path,
        chunk_size: int = copier.DEFAULT_CHUNK_SIZE,
        **kwargs: object,
    ) -> tuple[str, str]:
        # D-g3：verify_digests 新增可选 cancel_event 关键字参数，这里原样透传，
        # 断言点（fsync 先于 verify）保持不变。
        order.append("verify")
        return real_verify(src_path, dst_path, chunk_size, **kwargs)

    monkeypatch.setattr(os, "fsync", spying_fsync)
    monkeypatch.setattr(copier, "verify_digests", spying_verify)

    src = write_file(tmp_path / "src.bin", make_data(4096))
    copier.copy_file(src, tmp_path / "dst.bin")

    assert order == ["fsync", "verify"]


# --------------------------------------------------------------------------- #
# 同源防护（reviewer F1 回归：源与目标为同一文件时曾静默清空源数据）
# --------------------------------------------------------------------------- #


def assert_file_untouched(path: Path, data: bytes) -> None:
    """源文件必须字节未变、大小未变（即从未被 'wb' 截断）。"""
    assert path.read_bytes() == data
    assert sha256_of(path.read_bytes()) == sha256_of(data)
    assert path.stat().st_size == len(data)


def test_copy_file_onto_itself_is_rejected(tmp_path: Path) -> None:
    data = make_data(1000)
    src = write_file(tmp_path / "f", data)
    mtime_before = src.stat().st_mtime_ns

    with pytest.raises(copier.SameFileError, match="同一个文件"):
        copier.copy_file(src, src)

    assert_file_untouched(src, data)
    assert src.stat().st_mtime_ns == mtime_before


def test_copy_file_onto_itself_with_relative_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``./f`` 与 ``f`` 这类写法不同但指向同一文件的输入。"""
    data = make_data(4096)
    src = write_file(tmp_path / "f", data)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(copier.SameFileError):
        copier.copy_file(Path("f"), tmp_path / "f")

    assert_file_untouched(src, data)


def test_copy_file_onto_nonexistent_but_equivalent_path(tmp_path: Path) -> None:
    """目标路径不存在，但规范化后与源是同一位置（``ghost/../f``）。"""
    data = make_data(2048)
    src = write_file(tmp_path / "f", data)
    dst = tmp_path / "ghost" / ".." / "f"
    assert not dst.exists(), "前置条件：该写法指向的路径不存在（中间目录缺失）"

    with pytest.raises(copier.SameFileError):
        copier.copy_file(src, dst)

    assert_file_untouched(src, data)


def test_copy_file_onto_hardlink_of_source_is_rejected(tmp_path: Path) -> None:
    data = make_data(512)
    src = write_file(tmp_path / "f", data)
    link = tmp_path / "hardlink"
    try:
        os.link(src, link)
    except OSError:  # pragma: no cover - 文件系统不支持硬链接
        pytest.skip("文件系统不支持硬链接")

    with pytest.raises(copier.SameFileError):
        copier.copy_file(src, link)

    assert_file_untouched(src, data)


def test_copy_file_onto_symlink_of_source_is_rejected(tmp_path: Path) -> None:
    data = make_data(512)
    src = write_file(tmp_path / "f", data)
    link = tmp_path / "symlink"
    os.symlink(src, link)

    with pytest.raises(copier.SameFileError):
        copier.copy_file(src, link)

    assert_file_untouched(src, data)


def test_copy_onto_different_file_with_same_name_is_allowed(tmp_path: Path) -> None:
    """反向断言：不同目录下的同名文件不是同源，必须照常复制（避免误报）。"""
    data = make_data(300)
    src = write_file(tmp_path / "a" / "f", data)
    dst = write_file(tmp_path / "b" / "f", b"other")

    result = copier.copy_file(src, dst)

    assert result.size == len(data)
    assert dst.read_bytes() == data


def test_is_same_file_helper(tmp_path: Path) -> None:
    src = write_file(tmp_path / "f", b"x")
    other = write_file(tmp_path / "other", b"x")

    assert copier.is_same_file(src, src) is True
    assert copier.is_same_file(src, tmp_path / "ghost" / ".." / "f") is True
    assert copier.is_same_file(src, other) is False
    assert copier.is_same_file(src, tmp_path / "not-created-yet") is False


def test_canonical_path_normalizes(tmp_path: Path) -> None:
    src = write_file(tmp_path / "f", b"x")
    assert copier.canonical_path(tmp_path / "ghost" / ".." / "f") == copier.canonical_path(src)
    assert copier.canonical_path(src) == os.path.realpath(src)


def test_is_within_helper(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    inner = root / "sub"
    inner.mkdir(parents=True)

    assert copier.is_within(root, root) is True
    assert copier.is_within(inner, root) is True
    assert copier.is_within(root / "does-not-exist" / "deeper", root) is True
    assert copier.is_within(tmp_path / "outside", root) is False
    assert copier.is_within(root / ".." / "tree" / "sub", root) is True
