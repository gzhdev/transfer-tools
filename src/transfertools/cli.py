"""safecopy 命令行接口（design.md D5）。

用法::

    safecopy [-r] [--chunk-size MIB] [--no-clobber] [--no-preserve-metadata] <src...> <dst>

退出码：``0`` 全部成功 / ``1`` 参数或用法错误 / ``2`` 存在复制失败（含校验失败）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import copier, preflight
from .copier import SafeCopyError
from .preflight import UsageError

__all__ = ["EXIT_OK", "EXIT_USAGE", "EXIT_FAILURE", "PROG", "build_parser", "main"]

#: 全部成功。
EXIT_OK = 0
#: 参数 / 用法错误。
EXIT_USAGE = 1
#: 部分或全部文件复制失败（含完整性校验失败）。
EXIT_FAILURE = 2

PROG = "safecopy"

_EPILOG = """\
退出码:
  0  全部文件复制并通过 SHA-256 校验
  1  参数或用法错误（源不存在、未启用 -r 却传入目录、多源目标不是目录、
     递归目标等于源目录或位于源目录内部等）
  2  存在复制失败（源与目标为同一文件、写入/校验失败；其余文件仍会继续处理）

说明:
  复制通过「创建目标文件 → 分块顺序写入 → flush + fsync → 双端 SHA-256 比对」
  完成，不调用 shutil.copy / os.sendfile / CopyFile 等直接复制 API，以避免被安全
  软件的复制 API 钩子拦截。注意本工具只规避复制 API hook，不保证规避所有行为审计；
  外部存储上 fsync 可能较慢，属预期的安全代价。
  同源防护：源与目标指向同一文件（含路径写法不同、硬链接、符号链接）时报错退出且
  源文件零改动；递归模式下目标落在源目录内会被拒绝。
  既定行为：目标文件先于源数据创建，源不可读时目标上已存在的同名文件会先被截断，
  随后在失败清理中删除；需要保留该文件时请先用 --no-clobber 或确认源可读。
"""


#: 向后兼容的历史名称（实现已抽到 :mod:`transfertools.preflight`，GUI 复用同一套规则）。
_UsageError = UsageError


class _ArgumentParser(argparse.ArgumentParser):
    """把 argparse 默认的用法错误退出码 2 改为 1（design D5）。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: 错误: {message}\n")


def _chunk_size_mib(text: str) -> int:
    """把 ``--chunk-size`` 的 MiB 文本转成字节数并做 1~8 MiB 范围校验。"""
    try:
        mib = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"块大小必须是数字（MiB），收到 {text!r}") from None
    if mib != mib or mib in (float("inf"), float("-inf")):  # NaN / ±inf
        raise argparse.ArgumentTypeError(f"块大小必须是有限数字（MiB），收到 {text!r}")
    try:
        return copier.validate_chunk_size(int(mib * copier.MIB))
    except copier.InvalidChunkSizeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    """构造 safecopy 的参数解析器。"""
    parser = _ArgumentParser(
        prog=PROG,
        description="以「先创建目标文件、再分块写入、最后 SHA-256 校验」的方式复制文件，"
        "避开直接复制 API 被安全软件拦截。",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="递归复制源目录树到目标目录（源为目录时必须显式指定，否则报错）",
    )
    parser.add_argument(
        "--chunk-size",
        metavar="MIB",
        type=_chunk_size_mib,
        default=copier.DEFAULT_CHUNK_SIZE,
        help="分块写入的块大小，单位 MiB，范围 1~8，默认 4",
    )
    parser.add_argument(
        "--no-clobber",
        action="store_true",
        help="目标已存在时报错跳过，不截断覆盖（默认行为是截断覆盖）",
    )
    parser.add_argument(
        "--no-preserve-metadata",
        action="store_true",
        help="不保留源文件的权限与时间戳（目标文件系统不支持时本来就会降级并警告）",
    )
    parser.add_argument(
        "srcs",
        nargs="+",
        metavar="SRC",
        help="源文件；启用 -r 时为唯一的源目录",
    )
    parser.add_argument(
        "dst",
        metavar="DST",
        help="目标路径；已存在目录时复制为 <DST>/<源文件名>，多源复制要求它必须是已存在目录",
    )
    return parser


def _make_stdio_failure_tolerant() -> None:
    """让 stdio 在无法编码输出时降级为替换字符，而非抛 UnicodeEncodeError。

    在非 UTF-8 区域设置的 Windows 上（如 en-US 环境的 cp1252 管道），
    中文帮助/错误文本会触发编码异常导致退出码失真；保持本地编码、仅放宽
    错误处理可兼顾中文 Windows 控制台（GBK 正常显示）与异编码管道。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 防御性
                pass


def main(argv: list[str] | None = None) -> int:
    """CLI 入口，返回退出码（``0``/``1``/``2``）。"""
    _make_stdio_failure_tolerant()
    parser = build_parser()
    args = parser.parse_args(argv)

    srcs = [Path(item) for item in args.srcs]
    dst = Path(args.dst)

    try:
        _preflight(srcs, dst, recursive=args.recursive)
    except UsageError as exc:
        parser.error(str(exc))  # 不会返回：内部 sys.exit(EXIT_USAGE)

    if args.recursive:
        return _run_recursive(srcs[0], dst, args)
    return _run_flat(srcs, dst, args)


def _preflight(srcs: list[Path], dst: Path, *, recursive: bool) -> None:
    """复制前的用法校验：所有问题都在动手前一次性报出（不产生任何目标文件）。

    具体规则与消息文案来自 :mod:`transfertools.preflight`，GUI 复用同一套实现以
    保证两侧语义一致（design D-g3）。
    """
    if recursive:
        if len(srcs) != 1:
            raise UsageError("--recursive 只接受一个源目录")
        src = srcs[0]
        preflight.check_sources_exist(srcs)
        if not src.is_dir():
            raise UsageError(f"--recursive 要求源是目录: {src}")
        if dst.exists() and not dst.is_dir():
            raise UsageError(f"--recursive 要求目标是目录（或尚不存在的路径）: {dst}")
        # 同源防护（递归场景）：目标目录等于源目录、或落在源目录内部时，复制会把刚写出的
        # 文件再当作源继续遍历并覆盖源数据本身，因此在动手前直接拒绝。
        preflight.check_self_copy(src, dst)
        return

    preflight.check_sources_exist(srcs)
    preflight.check_directories_need_recursive(srcs)
    preflight.check_multi_source_destination(srcs, dst)


def _make_progress(src: Path, target: Path) -> copier.ProgressFn:
    """把阶段事件打印到 stderr，保持 stdout 只输出结果摘要。"""
    labels = {copier.STAGE_WRITE: "写入中", copier.STAGE_VERIFY: "校验中"}

    def progress(stage: str) -> None:
        label = labels.get(stage)
        if label:
            print(f"{label}: {src} -> {target}", file=sys.stderr)

    return progress


def _copy_one(src: Path, target: Path, args: argparse.Namespace) -> copier.CopyResult:
    return copier.copy_file(
        src,
        target,
        chunk_size=args.chunk_size,
        preserve_metadata=not args.no_preserve_metadata,
        warn=lambda message: print(f"警告: {message}", file=sys.stderr),
        progress=_make_progress(src, target),
    )


def _report_failure(src: Path, target: Path, exc: BaseException) -> None:
    print(f"错误: {src} -> {target}: {exc}", file=sys.stderr)


def _report_success(result: copier.CopyResult) -> None:
    print(
        f"完成: {result.dst}  大小={result.size}  sha256={result.sha256}",
        file=sys.stdout,
    )


def _run_flat(srcs: list[Path], dst: Path, args: argparse.Namespace) -> int:
    """单/多文件复制编排：逐文件处理、逐文件报错、不中断其余文件（design D5）。"""
    failures = 0
    for src in srcs:
        target = copier.resolve_destination(src, dst)
        if args.no_clobber and target.exists():
            print(
                f"错误: 目标已存在，--no-clobber 跳过: {target}",
                file=sys.stderr,
            )
            failures += 1
            continue
        try:
            result = _copy_one(src, target, args)
        except (SafeCopyError, OSError) as exc:
            _report_failure(src, target, exc)
            failures += 1
            continue
        _report_success(result)

    return EXIT_OK if failures == 0 else EXIT_FAILURE


def _run_recursive(src_dir: Path, dst_dir: Path, args: argparse.Namespace) -> int:
    """递归复制目录树：按相对路径重建结构，文件逐一走复制+校验流程。"""
    failures = 0
    for current, dirnames, filenames in os.walk(src_dir):
        dirnames.sort()
        current_path = Path(current)
        target_dir = dst_dir / current_path.relative_to(src_dir)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"错误: 创建目标目录失败 {target_dir}: {exc}", file=sys.stderr)
            failures += len(filenames) + 1
            dirnames[:] = []
            continue

        for name in sorted(filenames):
            src = current_path / name
            target = target_dir / name
            if args.no_clobber and target.exists():
                print(f"错误: 目标已存在，--no-clobber 跳过: {target}", file=sys.stderr)
                failures += 1
                continue
            try:
                result = _copy_one(src, target, args)
            except (SafeCopyError, OSError) as exc:
                _report_failure(src, target, exc)
                failures += 1
                continue
            _report_success(result)

    return EXIT_OK if failures == 0 else EXIT_FAILURE
