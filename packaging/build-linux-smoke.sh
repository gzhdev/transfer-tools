#!/usr/bin/env bash
# Linux 宿主替代验收（design D-g5）：onedir 冒烟构建 + 打包产物自检。
#
# PyInstaller 不支持交叉编译，Linux 上产不出 Windows exe；但 onedir 与 onefile 共用同一
# 个「分析 + 收集」阶段，因此在 Linux 上跑通可以提前暴露漏打包、缺 Qt 插件等问题。
# Windows exe 的实际产出由 .github/workflows/build-exe.yml 或 packaging/build-exe.ps1 完成。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BIN="dist/safecopy-gui/safecopy-gui"

echo "==> 1/3 onedir 冒烟构建（SAFECOPY_GUI_PACKAGE_MODE=onedir）"
SAFECOPY_GUI_PACKAGE_MODE=onedir uv run pyinstaller packaging/safecopy-gui.spec \
    --noconfirm --clean \
    --distpath dist \
    --workpath build/pyinstaller-build

if [ ! -x "$BIN" ]; then
    echo "构建失败：未找到可执行产物 $BIN" >&2
    exit 1
fi

echo "==> 2/3 产物 --help 冒烟"
QT_QPA_PLATFORM=offscreen "$BIN" --help

echo "==> 3/3 产物界面自检（无头构造主窗口）"
QT_QPA_PLATFORM=offscreen "$BIN" --self-test

echo "Linux onedir 冒烟构建通过：$BIN"
echo "注意：该产物是 Linux 目录形态，仅供打包链路验收；Windows 单文件 exe 需由 CI 或 Windows 本机构建。"
