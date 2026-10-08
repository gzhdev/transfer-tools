"""GUI 测试的公共装置（design D-g5）。

- **无头平台**：在任何 PySide6 导入之前设置 ``QT_QPA_PLATFORM=offscreen``，
  使界面测试可以在没有显示器的 Linux 宿主/CI 上运行；
- **QApplication 复用**：整个测试会话共用一个 ``QApplication``（Qt 不允许重复创建）；
- **等待辅助**：驱动 Qt 事件循环直到条件成立，用于「开始 → 完成 / 取消」这类
  跨线程流程（工作线程发信号，GUI 线程的槽在本进程事件循环里执行）。
"""

from __future__ import annotations

import os
import time

# 必须先于 PySide6 的导入完成设置，否则 Qt 会尝试连接真实显示服务。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Callable  # noqa: E402 - 紧随上面的环境设置
from typing import Any  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qt_app() -> Any:
    """会话级 ``QApplication``（未安装 PySide6 时整套 GUI 测试跳过）。"""
    pytest.importorskip(
        "PySide6", reason="未安装 GUI 额外依赖，请执行 `uv sync --extra gui`"
    )
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication(["pytest-safecopy-gui"])
    yield application


@pytest.fixture
def wait_for(qt_app: Any) -> Callable[..., bool]:
    """返回等待函数：``wait_for(predicate, timeout=...)`` 期间持续处理事件循环。"""

    def _wait(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qt_app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        qt_app.processEvents()
        return bool(predicate())

    return _wait


@pytest.fixture
def window(qt_app: Any) -> Any:
    """新建主窗口；测试结束确保工作线程停止后再销毁窗口。"""
    from transfertools.gui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.close()
    win.deleteLater()
    qt_app.processEvents()
