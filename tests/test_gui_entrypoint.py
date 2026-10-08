"""GUI 入口测试（tasks 2.2、spec「与 CLI 共存」）。

覆盖：``python -m transfertools.gui`` / ``safecopy-gui`` 控制台脚本的 ``--help``、
``--version``、``--self-test`` 与用法错误退出码；未安装 PySide6 时的中文安装提示；
以及「GUI 额外依赖不进入 CLI 运行时」这一硬约束。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from transfertools import __version__
from transfertools.gui import main as gui_main

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_python(args: list[str]) -> subprocess.CompletedProcess[str]:
    """在子进程运行（强制 offscreen + UTF-8 + 超时，避免无头宿主/编码差异与挂死）。

    ``encoding="utf-8"`` 与子进程的 ``PYTHONIOENCODING=utf-8`` 配对：否则在 Windows CI
    （默认 cp1252）上父进程会按本地编码解码中文输出。
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=120,
    )


def console_script_path() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return Path(sys.executable).parent / f"safecopy-gui{suffix}"


def block_pyside6(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ``import PySide6`` 失败：置 ``None`` 并清掉已缓存的全部子模块。

    ``sys.modules[name] = None`` 会使 ``import name`` 抛 ``ImportError``（"import of
    name halted; None in sys.modules"），是模拟「未安装该依赖」的标准手法。
    """
    for name in [name for name in sys.modules if name == "PySide6" or name.startswith("PySide6.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "PySide6", None)
    for name in [name for name in sys.modules if name.startswith("transfertools.gui")]:
        monkeypatch.delitem(sys.modules, name)


# --------------------------------------------------------------------------- #
# 模块入口 / 控制台脚本
# --------------------------------------------------------------------------- #


def test_module_entrypoint_help() -> None:
    completed = run_python(["-m", "transfertools.gui", "--help"])
    assert completed.returncode == gui_main.EXIT_OK
    assert "usage: safecopy-gui" in completed.stdout
    assert "--self-test" in completed.stdout
    assert "--version" in completed.stdout
    assert "退出码" in completed.stdout


def test_module_entrypoint_version() -> None:
    completed = run_python(["-m", "transfertools.gui", "--version"])
    assert completed.returncode == gui_main.EXIT_OK
    assert completed.stdout.strip() == f"safecopy-gui {__version__}"


def test_module_entrypoint_self_test_is_headless_safe() -> None:
    completed = run_python(["-m", "transfertools.gui", "--self-test"])
    assert completed.returncode == gui_main.EXIT_OK, completed.stderr
    assert "自检通过" in completed.stdout
    assert "Qt" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_console_script_entrypoint_help() -> None:
    script = console_script_path()
    if not script.exists():  # pragma: no cover - 取决于安装方式
        pytest.skip(f"未安装 console script: {script}")
    completed = subprocess.run(
        [str(script), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
    )
    assert completed.returncode == gui_main.EXIT_OK
    assert "usage: safecopy-gui" in completed.stdout


def test_module_entrypoint_usage_error_exit_code() -> None:
    completed = run_python(["-m", "transfertools.gui", "--no-such-option"])
    assert completed.returncode == gui_main.EXIT_USAGE
    assert "错误" in completed.stderr


# --------------------------------------------------------------------------- #
# 缺少 GUI 依赖时的友好提示（spec「缺失依赖提示」）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("argv", [["--self-test"]])
def test_missing_dependency_hint_exits_nonzero(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """模拟未安装 PySide6：中文提示 + 非零退出码，且没有未处理 traceback。

    必须把 ``PySide6`` **及其已缓存的子模块**一起清掉：Cython/C 导入层对已缓存的
    ``PySide6.QtWidgets`` 会直接命中快速路径，只把 ``sys.modules["PySide6"]`` 置为
    ``None`` 不足以复现「未安装」场景（而且会让 GUI 真正进入事件循环）。

    只测 ``--self-test``：该路径不会进入事件循环；无参数的启动路径放在子进程里验证。
    """
    block_pyside6(monkeypatch)

    code = gui_main.main(argv)

    assert code == gui_main.EXIT_MISSING_DEPS
    assert code != 0
    captured = capsys.readouterr()
    assert "缺少 GUI 依赖" in captured.err
    assert "PySide6" in captured.err
    assert 'uv sync --extra gui' in captured.err
    assert "safecopy" in captured.err, "提示里应说明 CLI 不受影响"
    assert "Traceback" not in captured.err


def test_missing_dependency_hint_for_plain_launch_in_subprocess() -> None:
    """无参数启动（会进入事件循环）的缺依赖路径：在子进程里验证，避免挂死测试会话。"""
    code = (
        "import sys\n"
        "sys.modules['PySide6'] = None\n"
        "from transfertools.gui.main import main\n"
        "raise SystemExit(main([]))\n"
    )
    completed = run_python(["-c", code])
    assert completed.returncode == gui_main.EXIT_MISSING_DEPS
    assert "缺少 GUI 依赖" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_self_test_in_process(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("PySide6", reason="未安装 GUI 额外依赖")
    assert gui_main.main(["--self-test"]) == gui_main.EXIT_OK
    assert "自检通过" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 与 CLI 共存：GUI 依赖不得进入 CLI 运行时
# --------------------------------------------------------------------------- #


def test_cli_import_does_not_pull_in_qt() -> None:
    code = (
        "import sys; import transfertools.cli; "
        "assert 'PySide6' not in sys.modules, 'CLI 不应导入 PySide6'; "
        "print('ok')"
    )
    completed = run_python(["-c", code])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_gui_package_import_does_not_pull_in_qt() -> None:
    code = (
        "import sys; import transfertools.gui; "
        "assert 'PySide6' not in sys.modules, '包导入期不应加载 Qt'; "
        "print('ok')"
    )
    completed = run_python(["-c", code])
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_cli_works_while_pyside6_unavailable() -> None:
    """即使在「PySide6 不可导入」的环境中，CLI 也必须照常工作（退出码不变）。"""
    code = (
        "import sys; sys.modules['PySide6'] = None; "
        "from transfertools.cli import main; "
        "raise SystemExit(main(['--help']))"
    )
    completed = run_python(["-c", code])
    assert completed.returncode == 0
    assert "usage: safecopy" in completed.stdout
