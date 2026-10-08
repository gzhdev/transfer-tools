"""打包与交付配置的静态校验（tasks 6.1~6.3、7.1）。

这些用例不需要 Windows/PyInstaller 就能验证「交付链路是否配齐且自洽」：
spec 的形态与开关、图标与版本资源、Windows 本机脚本、CI 工作流、README 说明，
以及 pyproject 中的入口与依赖分组。
"""

from __future__ import annotations

import struct
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "packaging" / "safecopy-gui.spec"
ICON_PATH = REPO_ROOT / "packaging" / "assets" / "safecopy-gui.ico"
VERSION_INFO_PATH = REPO_ROOT / "packaging" / "version_info.txt"
PS1_PATH = REPO_ROOT / "packaging" / "build-exe.ps1"
SMOKE_PATH = REPO_ROOT / "packaging" / "build-linux-smoke.sh"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-exe.yml"
README_PATH = REPO_ROOT / "README.md"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

sys.path.insert(0, str(REPO_ROOT / "packaging"))


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# PyInstaller 配置（tasks 6.1）
# --------------------------------------------------------------------------- #


def test_spec_is_valid_python_and_declares_onefile_windowed() -> None:
    text = SPEC_PATH.read_text(encoding="utf-8")
    compile(text, str(SPEC_PATH), "exec")  # 语法可编译（PyInstaller 会 exec 它）

    assert 'name="safecopy-gui"' in text
    assert "console=False" in text, "必须是 windowed（无控制台窗口）"
    assert "SAFECOPY_GUI_PACKAGE_MODE" in text, "需要支持 Linux onedir 冒烟形态"
    assert "COLLECT(" in text and "exclude_binaries=True" in text
    assert "version_info.txt" in text and "safecopy-gui.ico" in text


def test_spec_entry_script_shares_the_gui_entry() -> None:
    text = SPEC_PATH.read_text(encoding="utf-8")
    assert "safecopy_gui_launcher.py" in text
    launcher = (REPO_ROOT / "packaging" / "safecopy_gui_launcher.py").read_text(encoding="utf-8")
    assert "from transfertools.gui.main import main" in launcher
    assert "sys.exit(main())" in launcher


def test_icon_asset_is_a_real_multi_size_ico() -> None:
    data = ICON_PATH.read_bytes()
    reserved, image_type, count = struct.unpack("<HHH", data[:6])
    assert (reserved, image_type) == (0, 1), "ICO 头部应为 0/1"
    assert count >= 4, "至少包含多个尺寸"

    sizes = []
    for index in range(count):
        entry = struct.unpack("<BBBBHHII", data[6 + 16 * index : 22 + 16 * index])
        width, height, _, _, planes, bit_count, length, offset = entry
        assert planes == 1 and bit_count == 32
        assert offset + length <= len(data), "图像数据必须落在文件内"
        sizes.append((width or 256, height or 256))
    assert sizes == sorted(sizes)
    assert (16, 16) in sizes and (128, 128) in sizes


def test_committed_icon_matches_generator(tmp_path: Path) -> None:
    """占位图标必须与脚本产物逐字节一致（脚本生成、可复现，便于后续替换）。"""
    import make_icon

    target = tmp_path / "generated.ico"
    assert make_icon.main([str(target)]) == 0
    assert target.read_bytes() == ICON_PATH.read_bytes()


def test_version_resource_matches_package_version(pyproject: dict) -> None:
    text = VERSION_INFO_PATH.read_text(encoding="utf-8")
    version = pyproject["project"]["version"]
    assert "VSVersionInfo(" in text
    assert f'StringStruct("FileVersion", "{version}")' in text
    assert f'StringStruct("ProductVersion", "{version}")' in text
    assert "safecopy-gui.exe" in text
    assert "safecopy-gui" in text
    assert "LGPL" in text, "版本资源里应带许可提示"


# --------------------------------------------------------------------------- #
# Windows 本机脚本（tasks 6.2）
# --------------------------------------------------------------------------- #


def test_windows_build_script_exists_and_is_complete() -> None:
    text = PS1_PATH.read_text(encoding="utf-8")
    assert text.startswith("#Requires -Version 5.1")
    assert "[CmdletBinding()]" in text and "param(" in text
    assert '.venv-win' in text, "应使用独立虚拟环境，避免污染开发环境"
    assert '".[gui]"' in text and "pyinstaller>=6.0" in text
    assert "pytest" in text, "默认应跑测试套件"
    assert "-SkipTests" in text and "-Clean" in text
    assert "packaging\\safecopy-gui.spec" in text
    assert "dist\\safecopy-gui.exe" in text
    assert "$ErrorActionPreference = \"Stop\"" in text and "throw " in text
    assert "--noconfirm" in text and "--clean" in text


def test_linux_smoke_script_uses_onedir_mode() -> None:
    text = SMOKE_PATH.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "SAFECOPY_GUI_PACKAGE_MODE=onedir" in text
    assert "packaging/safecopy-gui.spec" in text
    assert "dist/safecopy-gui/safecopy-gui" in text
    assert "--self-test" in text and "--help" in text
    assert "Windows" in text, "脚本应说明 Linux 产物不是 Windows exe"


# --------------------------------------------------------------------------- #
# CI 工作流（tasks 6.3）
# --------------------------------------------------------------------------- #


def test_workflow_is_valid_yaml_with_expected_pipeline() -> None:
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    # YAML 1.1 里裸写的 on 会被解析成布尔 True。
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["tags"] == ["v*"]
    assert triggers["workflow_dispatch"] is None

    job = data["jobs"]["build-gui-exe"]
    assert job["runs-on"] == "windows-latest"

    steps = job["steps"]
    names = [step.get("name", step.get("uses", "")) for step in steps]
    runs = "\n".join(step.get("run", "") for step in steps)
    uses = " ".join(step.get("uses", "") for step in steps)

    assert "setup-python" in uses
    assert 'python-version: "3.13"' in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "pip install" in runs and "[gui]" in runs and "pyinstaller" in runs
    assert "pytest" in runs, "CI 必须先跑测试"
    assert "PyInstaller packaging/safecopy-gui.spec" in runs
    assert "actions/upload-artifact" in uses
    assert "dist/safecopy-gui.exe" in runs

    artifact = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact"))
    assert artifact["with"]["path"] == "dist/safecopy-gui.exe"
    assert artifact["with"]["if-no-files-found"] == "error"
    assert any("offscreen" in str(step.get("env", {})) for step in steps), (
        "CI 需要 offscreen 才能跑界面测试"
    )
    assert job["name"].startswith("构建 safecopy-gui.exe")


# --------------------------------------------------------------------------- #
# pyproject / README（tasks 2.1、7.1）
# --------------------------------------------------------------------------- #


def test_pyproject_declares_gui_entry_extra_and_packaging_dev_deps(pyproject: dict) -> None:
    scripts = pyproject["project"]["scripts"]
    assert scripts["safecopy"] == "transfertools.cli:main"
    assert scripts["safecopy-gui"] == "transfertools.gui.main:main"

    extra = pyproject["project"]["optional-dependencies"]["gui"]
    assert any(spec.lower().startswith("pyside6") for spec in extra)
    # CLI 运行时不新增依赖（design D-g1）。
    assert pyproject["project"]["dependencies"] == []

    dev = pyproject["dependency-groups"]["dev"]
    joined = " ".join(dev).lower()
    assert "pyinstaller" in joined
    assert "pyside6" in joined, "开发环境要能跑 offscreen 界面测试"


def test_readme_documents_gui_usage_licensing_and_packaging() -> None:
    text = README_PATH.read_text(encoding="utf-8")
    assert "uv sync --extra gui" in text, "安装方式"
    assert "safecopy-gui" in text
    assert "LGPL" in text, "PySide6/Qt 许可与合规说明"
    assert "safecopy-gui.exe" in text and "build-exe.ps1" in text, "Windows 打包方式"
    assert "build-exe.yml" in text and "artifact" in text.lower(), "CI 产物获取方式"
    assert "onedir" in text and "交叉编译" in text, "Linux 宿主边界声明"
    assert "build-linux-smoke.sh" in text, "本机替代验收脚本"
