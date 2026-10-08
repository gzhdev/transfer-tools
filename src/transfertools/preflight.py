"""复制前的用法校验：CLI 与 GUI 共用，保证两侧语义一致（design.md D-g3）。

这里只放「与界面无关、可在动手复制前一次性判定」的规则，消息文案与既有 CLI 输出
保持逐字一致（``tests/test_cli.py`` 依赖这些文案）：

- 源路径必须存在；
- 源为目录时必须显式启用递归（CLI ``-r`` / GUI「递归复制目录」复选框）；
- 递归复制时目标不得等于源目录或位于源目录内部（自复制会覆盖源数据）；
- CLI 的多源复制要求目标本身是已存在的目录。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import copier

__all__ = [
    "UsageError",
    "check_sources_exist",
    "check_directories_need_recursive",
    "check_self_copy",
    "check_multi_source_destination",
]


class UsageError(Exception):
    """用法错误。

    CLI 侧统一转成退出码 1（``parser.error``）；GUI 侧在开始复制前一次性提示，
    不产生任何目标文件。
    """


def check_sources_exist(srcs: Sequence[Path]) -> None:
    """所有源路径都必须存在。

    :raises UsageError: 存在失效路径（消息与 CLI 一致）。
    """
    missing = [str(item) for item in srcs if not item.exists()]
    if missing:
        raise UsageError("源路径不存在: " + ", ".join(missing))


def check_directories_need_recursive(srcs: Sequence[Path]) -> None:
    """源列表中不允许出现目录，除非显式启用递归。

    :raises UsageError: 源列表包含目录（消息与 CLI 一致）。
    """
    directories = [str(item) for item in srcs if item.is_dir()]
    if directories:
        raise UsageError(
            "源是目录，请加 -r/--recursive 显式启用递归复制: " + ", ".join(directories)
        )


def check_self_copy(src_dir: Path, dst_dir: Path) -> None:
    """递归复制的自复制防护。

    目标目录等于源目录或落在源目录内部时，复制会把刚写出的文件再当作源继续遍历并
    覆盖源数据本身，因此在动手前直接拒绝。

    :raises UsageError: 目标与源目录相同或位于源目录内部（消息与 CLI 一致）。
    """
    if copier.is_within(dst_dir, src_dir):
        raise UsageError(
            "目标目录与源目录相同或位于源目录内部（会自我复制并覆盖源数据），"
            f"已拒绝: {src_dir} -> {dst_dir}"
        )


def check_multi_source_destination(srcs: Sequence[Path], dst: Path) -> None:
    """多源复制的目标必须是已存在的目录。

    :raises UsageError: 多个源但目标不是已存在目录（消息与 CLI 一致）。
    """
    if len(srcs) > 1 and not dst.is_dir():
        raise UsageError(f"多源复制要求最后一个是已存在的目录: {dst}")
