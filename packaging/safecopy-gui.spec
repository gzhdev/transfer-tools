# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（design D-g4）：onefile + windowed → ``safecopy-gui.exe``。

默认形态（Windows 本机 / CI）::

    pyinstaller packaging/safecopy-gui.spec --noconfirm --clean
    # 产物: dist/safecopy-gui.exe（无控制台窗口、内嵌版本资源与图标）

Linux 宿主的替代验收（PyInstaller 不支持交叉编译，见 design D-g5）::

    SAFECOPY_GUI_PACKAGE_MODE=onedir pyinstaller packaging/safecopy-gui.spec --noconfirm --clean
    # 产物: dist/safecopy-gui/（目录形态），随后可执行 dist/safecopy-gui/safecopy-gui --self-test

``--onedir`` 这类命令行开关在传入 .spec 时会被忽略，因此这里用环境变量
``SAFECOPY_GUI_PACKAGE_MODE``（``onefile`` 默认 / ``onedir``）切换形态，
让同一份配置同时服务于 Windows 交付与 Linux 冒烟构建。
"""

from __future__ import annotations

import os
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - PyInstaller 注入的全局变量
PROJECT_ROOT = SPEC_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
ASSETS_DIR = SPEC_DIR / "assets"
ICON_PATH = ASSETS_DIR / "safecopy-gui.ico"
VERSION_FILE = SPEC_DIR / "version_info.txt"

MODE = os.environ.get("SAFECOPY_GUI_PACKAGE_MODE", "onefile").strip().lower()
ONEFILE = MODE != "onedir"

# 入口脚本（与 safecopy-gui / python -m transfertools.gui 同一实现）。
entry_script = str(SPEC_DIR / "safecopy_gui_launcher.py")

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [entry_script],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=[],
    # PySide6 的 Qt 插件（platforms/styles/imageformats 等）由 pyinstaller-hooks-contrib
    # 的官方 hook 自动收集；这里只需保证只导入 QtCore/QtGui/QtWidgets，
    # 未导入的 QtWebEngine 等大模块不会被带进产物。
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除与本工具无关的重型标准库/第三方模块，减小体积。
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia",
        "PySide6.Qt3DCore",
        "PySide6.QtCharts",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821 - PyInstaller 注入

exe_kwargs = dict(
    name="safecopy-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed：不弹控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 图标与版本资源仅 Windows 使用，其它平台由 PyInstaller 忽略。
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)

if ONEFILE:
    exe = EXE(  # noqa: F821 - PyInstaller 注入
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        runtime_tmpdir=None,
        **exe_kwargs,
    )
else:
    # onedir（Linux 冒烟验收）：EXE 只含引导程序，二进制与数据由 COLLECT 收集。
    exe = EXE(  # noqa: F821 - PyInstaller 注入
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **exe_kwargs,
    )
    coll = COLLECT(  # noqa: F821 - PyInstaller 注入
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="safecopy-gui",
    )
