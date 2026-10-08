"""静态与运行时双重断言：实现中不存在任何直接复制 API（design D1、tasks 2.1）。

任务 2.1 要求「可用 grep 验证无这些调用」。这里用 AST 分析代替纯文本 grep，
避免把文档字符串里对 `shutil.copy` 的说明误判成真实调用。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import transfertools

PACKAGE_DIR = Path(transfertools.__file__).parent
# 覆盖顶层模块与 GUI 子包（GUI 复用同一复制引擎，同样不得引入直接复制 API）。
SOURCE_FILES = sorted(PACKAGE_DIR.glob("*.py")) + sorted((PACKAGE_DIR / "gui").glob("*.py"))

#: 被禁用的复制 API（模块名 / os 属性名）。
FORBIDDEN_MODULES = {"shutil"}
FORBIDDEN_ATTRS = {"sendfile", "copy_file_range", "copyfile", "copy2", "copyfileobj", "copytree"}


def test_package_has_sources() -> None:
    assert {path.name for path in SOURCE_FILES} >= {
        "__init__.py",
        "__main__.py",
        "cli.py",
        "copier.py",
    }


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda path: path.name)
def test_no_forbidden_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in FORBIDDEN_MODULES, f"{path.name}: import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in FORBIDDEN_MODULES, f"{path.name}: from {node.module} import ..."


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda path: path.name)
def test_no_forbidden_os_calls(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_MODULES, f"{path.name}: 引用了 {node.id}"
        elif isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_ATTRS, f"{path.name}: 调用了 .{node.attr}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id != "getattr":
                continue
            # 允许 getattr(os, "posix_fallocate", None) 这类能力探测，
            # 但禁止用字符串把复制 API 藏起来。
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                literal = node.args[1].value
                assert literal not in FORBIDDEN_ATTRS, f"{path.name}: getattr({literal!r})"
                assert literal not in FORBIDDEN_MODULES, f"{path.name}: getattr({literal!r})"


def test_no_shutil_reference_in_any_form() -> None:
    """兜底：整个包里不应出现 shutil 模块的导入或属性访问。"""
    for path in SOURCE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "shutil" not in names


def test_copier_module_surface_is_read_write_only() -> None:
    """运行时确认包内没有直接暴露复制语义的函数名。"""
    from transfertools import copier

    public = {name for name in dir(copier) if not name.startswith("_")}
    assert not (public & FORBIDDEN_ATTRS)
    assert not (public & FORBIDDEN_MODULES)
    assert hasattr(copier, "copy_file")
