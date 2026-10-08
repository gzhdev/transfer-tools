"""PyInstaller 打包入口（design D-g4）。

与 ``safecopy-gui`` 控制台脚本、``python -m transfertools.gui`` 走同一条代码路径：
:func:`transfertools.gui.main.main`。

之所以不直接用 ``src/transfertools/gui/__main__.py`` 作打包入口，是因为 PyInstaller 会把
入口脚本当作顶层 ``__main__`` 执行，该文件里的相对导入（``from .main import main``）
在无包上下文的场景下会失败；这里用绝对导入代替，行为完全一致。
"""

from __future__ import annotations

import sys

from transfertools.gui.main import main

if __name__ == "__main__":
    sys.exit(main())
