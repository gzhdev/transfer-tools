"""safecopy 的 PySide6 图形界面（design.md D-g1，可选依赖 ``transfertools[gui]``）。

分层（design D-g3）：

- :mod:`transfertools.gui.controller`：不依赖 Qt 的编排层（预检、展开、逐文件复制、取消）；
- :mod:`transfertools.gui.worker`：``QObject`` 工作线程，把控制器回调桥接为 Qt 信号；
- :mod:`transfertools.gui.main_window`：单窗口三段式界面（中文文案）与状态机；
- :mod:`transfertools.gui.main`：入口（``safecopy-gui`` 控制台脚本 / ``python -m transfertools.gui``）。

本包**不在导入期引入 PySide6**：未安装 GUI 额外依赖时，``import transfertools.gui``
与 ``safecopy`` CLI 都照常工作，只有真正启动界面时才给出缺失依赖提示。
"""

from __future__ import annotations

__all__: list[str] = []
