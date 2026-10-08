"""``python -m transfertools.gui`` 入口（tasks 2.2）。

与 ``safecopy-gui`` 控制台脚本、PyInstaller 打包入口共用 :func:`transfertools.gui.main.main`。
"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
