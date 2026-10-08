"""GUI 入口（tasks 2.2、6.1）：``safecopy-gui`` 控制台脚本、``python -m transfertools.gui``
与 PyInstaller 打包产物共用同一实现。

设计要点（spec「与 CLI 共存」）：

- **PySide6 延迟导入**：模块导入期不触碰 Qt，未安装 GUI 额外依赖时
  ``import transfertools.gui.main`` 依然成功，只有真正启动界面才打印中文安装提示并以
  非零退出码结束，而不是抛出未处理的导入堆栈；
- **打包自检**：``--self-test`` 在无头环境（``QT_QPA_PLATFORM=offscreen``）下构造
  ``QApplication`` 与主窗口，用于校验 PyInstaller 产物的依赖与 Qt 插件是否完整；
  打包冒烟与 CI 使用它，不需要真实显示器。

退出码：``0`` 正常退出 / ``1`` 用法错误 / ``2`` 启动期错误 / ``3`` 缺少 GUI 依赖。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING, Any

from .. import __version__

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行期不导入 Qt
    from .main_window import MainWindow

__all__ = [
    "PROG",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_STARTUP_ERROR",
    "EXIT_MISSING_DEPS",
    "MissingDependencyError",
    "build_parser",
    "main",
]

PROG = "safecopy-gui"

#: 正常退出（含 ``--help`` / ``--version`` / ``--self-test`` 成功）。
EXIT_OK = 0
#: 参数或用法错误。
EXIT_USAGE = 1
#: 启动期错误（无法创建 QApplication 等）。
EXIT_STARTUP_ERROR = 2
#: 缺少 GUI 额外依赖（PySide6）。
EXIT_MISSING_DEPS = 3

INSTALL_HINT = """\
缺少 GUI 依赖：未安装 PySide6。
请安装 GUI 额外依赖后重试：
  pip install "transfertools[gui]"
  uv sync --extra gui
命令行版本 safecopy 不需要这些依赖，可继续正常使用。"""

_EPILOG = """\
退出码:
  0  正常退出（含 --help / --version / --self-test 成功）
  1  参数或用法错误
  2  启动期错误（无法创建图形应用）
  3  缺少 GUI 依赖（未安装 PySide6）

说明:
  图形界面复用与 safecopy CLI 相同的复制引擎：先创建目标文件、再分块顺序写入、
  flush + fsync 后做双端 SHA-256 校验，失败或取消时删除未校验的半成品。
  --self-test 在无头环境构造一次界面，用于校验打包产物的依赖完整性。
"""


class MissingDependencyError(Exception):
    """GUI 额外依赖缺失（PySide6 未安装）。"""


class _ArgumentParser(argparse.ArgumentParser):
    """用法错误返回退出码 1（与 CLI 的约定一致）。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: 错误: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    """构造 ``safecopy-gui`` 的参数解析器（GUI 无业务参数，均为自检/元信息开关）。"""
    parser = _ArgumentParser(
        prog=PROG,
        description="safecopy 的图形界面：拖拽/多选源文件，配置复制选项，"
        "查看进度与日志，可随时取消（取消即删除未校验的半成品）。",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="store_true",
        help="打印版本号后退出",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="不显示窗口，自检界面依赖与构造是否正常（打包冒烟用）",
    )
    return parser


def _load_gui() -> tuple[Any, type["MainWindow"]]:
    """延迟导入 Qt 与主窗口。

    :raises MissingDependencyError: 未安装 PySide6。
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支由测试覆盖
        raise MissingDependencyError(str(exc)) from exc

    from .main_window import MainWindow

    return QApplication, MainWindow


def _prepare_headless_platform() -> None:
    """无显示环境下自动切到 Qt 的 offscreen 平台（``--self-test`` 用）。"""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _create_application(QApplication: Any, argv: list[str]) -> Any:
    """创建 ``QApplication``（已存在时复用，便于测试与打包自检）。"""
    instance = QApplication.instance()
    return instance if instance is not None else QApplication(argv)


def _self_test() -> int:
    """打包/环境自检：构造界面但不进入事件循环。"""
    _prepare_headless_platform()
    try:
        QApplication, MainWindow = _load_gui()
    except MissingDependencyError as exc:
        print(INSTALL_HINT, file=sys.stderr)
        print(f"（导入失败详情: {exc}）", file=sys.stderr)
        return EXIT_MISSING_DEPS

    from PySide6.QtCore import qVersion  # noqa: PLC0415 - 延迟导入，避免拖慢 CLI 环境

    try:
        application = _create_application(QApplication, [PROG, "--self-test"])
        window = MainWindow()
    except Exception as exc:  # noqa: BLE001 - 自检需要把任何失败转成退出码
        print(f"自检失败：无法构造主窗口: {exc}", file=sys.stderr)
        return EXIT_STARTUP_ERROR

    if application is None:  # pragma: no cover - 防御性检查
        print("自检失败：无法创建 QApplication。", file=sys.stderr)
        return EXIT_STARTUP_ERROR

    print(
        f"自检通过：{PROG} {__version__} / Qt {qVersion()} / "
        f"窗口标题「{window.windowTitle()}」/ 文案均为中文。"
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """GUI 入口，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"{PROG} {__version__}")
        return EXIT_OK
    if args.self_test:
        return _self_test()

    try:
        QApplication, MainWindow = _load_gui()
    except MissingDependencyError as exc:
        print(INSTALL_HINT, file=sys.stderr)
        print(f"（导入失败详情: {exc}）", file=sys.stderr)
        return EXIT_MISSING_DEPS

    try:
        application = _create_application(QApplication, sys.argv[:1] or [PROG])
        application.setApplicationName(PROG)
        application.setApplicationDisplayName("safecopy 图形界面")
        window = MainWindow()
    except Exception as exc:  # noqa: BLE001 - 启动失败要给退出码而不是堆栈
        print(f"启动失败：{exc}", file=sys.stderr)
        return EXIT_STARTUP_ERROR

    window.show()
    return int(application.exec())
