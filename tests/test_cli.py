"""``transfertools.cli`` 的集成测试：参数校验、单/多文件编排、退出码、递归目录。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from transfertools import cli, copier

MIB = copier.MIB

IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "usage: safecopy" in out
    assert "--chunk-size" in out and "--recursive" in out and "--no-clobber" in out
    assert "退出码" in out


def test_module_entrypoint_works() -> None:
    """`python -m transfertools --help`（tasks 3.4）。"""
    completed = subprocess.run(
        [sys.executable, "-m", "transfertools", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == cli.EXIT_OK
    assert "usage: safecopy" in completed.stdout


def test_console_script_entrypoint_works() -> None:
    """`[project.scripts] safecopy` 已安装（tasks 1.1）。"""
    script = Path(sys.executable).parent / "safecopy"
    if not script.exists():  # pragma: no cover - 依赖安装方式
        pytest.skip("console script 未安装到当前解释器环境")
    completed = subprocess.run(
        [str(script), "--help"], capture_output=True, text=True, check=False
    )
    assert completed.returncode == cli.EXIT_OK
    assert "usage: safecopy" in completed.stdout


@pytest.mark.parametrize("value", ["0", "9", "8.5", "abc", "nan"])
def test_invalid_chunk_size_is_usage_error(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--chunk-size", value, "a", "b"])
    assert excinfo.value.code == cli.EXIT_USAGE
    assert "块大小" in capsys.readouterr().err


def test_too_few_arguments_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["only-one-argument"])
    assert excinfo.value.code == cli.EXIT_USAGE
    assert capsys.readouterr().err != ""


# --------------------------------------------------------------------------- #
# 单文件复制（spec 场景「单文件复制成功」「单源复制到目录」）
# --------------------------------------------------------------------------- #


def test_single_file_to_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = b"hello safecopy" * 100
    src = write_file(tmp_path / "src.txt", data)
    dst = tmp_path / "dst.txt"

    assert cli.main([str(src), str(dst)]) == cli.EXIT_OK

    assert dst.read_bytes() == data
    stdout = capsys.readouterr().out
    assert "完成:" in stdout
    assert f"sha256={hashlib.sha256(data).hexdigest()}" in stdout


def test_single_file_to_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = b"payload"
    src = write_file(tmp_path / "src.txt", data)
    target_dir = tmp_path / "out"
    target_dir.mkdir()

    assert cli.main([str(src), str(target_dir)]) == cli.EXIT_OK

    assert (target_dir / "src.txt").read_bytes() == data
    capsys.readouterr()


def test_chunk_size_option_is_used(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = os.urandom(2 * MIB + 17)
    src = write_file(tmp_path / "big.bin", data)
    dst = tmp_path / "out.bin"

    assert cli.main(["--chunk-size", "1", str(src), str(dst)]) == cli.EXIT_OK

    assert dst.read_bytes() == data
    capsys.readouterr()


def test_progress_goes_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write_file(tmp_path / "src.txt", b"abc")
    assert cli.main([str(src), str(tmp_path / "dst.txt")]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "写入中" in captured.err and "校验中" in captured.err
    assert "写入中" not in captured.out


def test_overwrite_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = write_file(tmp_path / "src.txt", b"new")
    dst = write_file(tmp_path / "dst.txt", b"older and longer content")

    assert cli.main([str(src), str(dst)]) == cli.EXIT_OK

    assert dst.read_bytes() == b"new"
    capsys.readouterr()


def test_no_clobber_skips_existing_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = write_file(tmp_path / "src.txt", b"new")
    dst = write_file(tmp_path / "dst.txt", b"original")

    assert cli.main(["--no-clobber", str(src), str(dst)]) == cli.EXIT_FAILURE

    assert dst.read_bytes() == b"original"
    assert "--no-clobber" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 多文件复制（spec 场景「多源复制到目录」「参数错误」）
# --------------------------------------------------------------------------- #


def test_multi_file_to_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target_dir = tmp_path / "out"
    target_dir.mkdir()
    sources = []
    for index in range(3):
        src = write_file(tmp_path / f"src{index}.bin", os.urandom(4096 + index))
        sources.append(src)

    assert cli.main([*(str(src) for src in sources), str(target_dir)]) == cli.EXIT_OK

    for src in sources:
        assert (target_dir / src.name).read_bytes() == src.read_bytes()
    capsys.readouterr()


def test_multi_file_target_must_be_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_a = write_file(tmp_path / "a.txt", b"a")
    src_b = write_file(tmp_path / "b.txt", b"b")
    target = tmp_path / "not-a-dir.txt"

    with pytest.raises(SystemExit) as excinfo:
        cli.main([str(src_a), str(src_b), str(target)])
    assert excinfo.value.code == cli.EXIT_USAGE

    assert not target.exists()
    assert "目录" in capsys.readouterr().err


def test_missing_source_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target_dir = tmp_path / "out"
    target_dir.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        cli.main([str(tmp_path / "nope.txt"), str(target_dir)])
    assert excinfo.value.code == cli.EXIT_USAGE

    assert list(target_dir.iterdir()) == []
    assert "源路径不存在" in capsys.readouterr().err


def test_one_missing_source_produces_no_target_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """spec「参数错误」：不产生任何目标文件。"""
    good = write_file(tmp_path / "good.txt", b"good")
    target_dir = tmp_path / "out"
    target_dir.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        cli.main([str(good), str(tmp_path / "missing.txt"), str(target_dir)])
    assert excinfo.value.code == cli.EXIT_USAGE

    assert list(target_dir.iterdir()) == []
    capsys.readouterr()


def test_partial_failure_returns_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """逐文件报错、不中断其余文件，最终退出码 2（tasks 3.3）。"""
    first = write_file(tmp_path / "first.txt", b"first")
    second = write_file(tmp_path / "second.txt", b"second")
    target_dir = tmp_path / "out"
    target_dir.mkdir()

    real_copy = copier.copy_file

    def flaky_copy(src, dst, **kwargs):
        if Path(src).name == "first.txt":
            raise copier.VerificationError("模拟校验失败")
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(copier, "copy_file", flaky_copy)

    assert cli.main([str(first), str(second), str(target_dir)]) == cli.EXIT_FAILURE

    assert not (target_dir / "first.txt").exists()
    assert (target_dir / "second.txt").read_bytes() == b"second"
    err = capsys.readouterr().err
    assert "模拟校验失败" in err


@pytest.mark.skipif(IS_ROOT, reason="root 不受文件权限限制")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 权限基于 ACL，chmod 0o000 不能阻止读取，本用例语义仅适用于 POSIX 平台",
)
def test_unreadable_source_returns_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = write_file(tmp_path / "blocked.txt", b"secret")
    ok = write_file(tmp_path / "ok.txt", b"fine")
    target_dir = tmp_path / "out"
    target_dir.mkdir()
    os.chmod(blocked, 0o000)

    try:
        assert cli.main([str(blocked), str(ok), str(target_dir)]) == cli.EXIT_FAILURE
    finally:
        os.chmod(blocked, 0o600)

    assert not (target_dir / "blocked.txt").exists()
    assert (target_dir / "ok.txt").read_bytes() == b"fine"
    capsys.readouterr()


def test_verification_failure_reports_and_cleans(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端确认校验失败时 CLI 报错、目标不残留（spec 场景「校验不一致」）。"""
    src = write_file(tmp_path / "src.bin", os.urandom(2048))
    dst = tmp_path / "dst.bin"

    monkeypatch.setattr(copier, "verify_digests", lambda *a, **k: ("0" * 64, "1" * 64))

    assert cli.main([str(src), str(dst)]) == cli.EXIT_FAILURE

    assert not dst.exists()
    err = capsys.readouterr().err
    assert "SHA-256" in err and "模拟" not in err


# --------------------------------------------------------------------------- #
# 递归目录复制（spec 需求「递归目录复制（可选）」）
# --------------------------------------------------------------------------- #


def test_recursive_copies_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src_dir = tmp_path / "tree"
    write_file(src_dir / "top.txt", b"top")
    write_file(src_dir / "sub" / "nested.bin", os.urandom(MIB + 5))
    write_file(src_dir / "sub" / "deep" / "deeper.txt", b"deeper")
    (src_dir / "empty").mkdir()
    dst_dir = tmp_path / "copied"

    assert cli.main(["-r", str(src_dir), str(dst_dir)]) == cli.EXIT_OK

    for relative in ("top.txt", "sub/nested.bin", "sub/deep/deeper.txt"):
        assert (dst_dir / relative).read_bytes() == (src_dir / relative).read_bytes()
    assert (dst_dir / "empty").is_dir()
    assert len(capsys.readouterr().out.splitlines()) == 3


def test_directory_source_without_recursive_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src_dir = tmp_path / "tree"
    src_dir.mkdir()
    dst_dir = tmp_path / "copied"

    with pytest.raises(SystemExit) as excinfo:
        cli.main([str(src_dir), str(dst_dir)])
    assert excinfo.value.code == cli.EXIT_USAGE

    assert not dst_dir.exists()
    assert "--recursive" in capsys.readouterr().err


def test_recursive_requires_single_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["-r", str(first), str(second), str(tmp_path / "out")])
    assert excinfo.value.code == cli.EXIT_USAGE
    assert "只接受一个源目录" in capsys.readouterr().err


def test_recursive_source_must_be_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = write_file(tmp_path / "file.txt", b"x")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["-r", str(src), str(tmp_path / "out")])
    assert excinfo.value.code == cli.EXIT_USAGE
    assert capsys.readouterr().err != ""


def test_recursive_reports_failure_for_one_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    src_dir = tmp_path / "tree"
    write_file(src_dir / "good.txt", b"good")
    write_file(src_dir / "bad.txt", b"bad")
    dst_dir = tmp_path / "copied"

    real_copy = copier.copy_file

    def flaky_copy(src, dst, **kwargs):
        if Path(src).name == "bad.txt":
            raise copier.VerificationError("模拟校验失败")
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(copier, "copy_file", flaky_copy)

    assert cli.main(["-r", str(src_dir), str(dst_dir)]) == cli.EXIT_FAILURE

    assert (dst_dir / "good.txt").read_bytes() == b"good"
    assert not (dst_dir / "bad.txt").exists()
    capsys.readouterr()


def test_recursive_with_no_clobber(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src_dir = tmp_path / "tree"
    write_file(src_dir / "a.txt", b"new")
    dst_dir = tmp_path / "copied"
    write_file(dst_dir / "a.txt", b"old")

    assert cli.main(["-r", "--no-clobber", str(src_dir), str(dst_dir)]) == cli.EXIT_FAILURE

    assert (dst_dir / "a.txt").read_bytes() == b"old"
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# 同源防护的 CLI 回归（reviewer F1）
# --------------------------------------------------------------------------- #


def tree_snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_safecopy_file_onto_itself_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`safecopy f f` 必须报错退出，且 f 内容原样保留（曾静默清零）。"""
    data = b"important payload" * 64
    src = write_file(tmp_path / "f", data)
    digest_before = hashlib.sha256(data).hexdigest()

    assert cli.main([str(src), str(src)]) == cli.EXIT_FAILURE

    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest_before
    assert "同一个文件" in capsys.readouterr().err


def test_safecopy_file_onto_itself_other_spelling_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对写法与绝对写法指向同一文件时同样拦截。"""
    data = b"payload"
    src = write_file(tmp_path / "f", data)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["f", str(tmp_path / "f")]) == cli.EXIT_FAILURE

    assert src.read_bytes() == data
    capsys.readouterr()


def test_safecopy_file_onto_itself_as_directory_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`safecopy f .`：目标解析为 ./f，即源自身，同样必须拦截。"""
    data = b"payload"
    src = write_file(tmp_path / "f", data)
    monkeypatch.chdir(tmp_path)

    assert cli.main(["f", "."]) == cli.EXIT_FAILURE

    assert src.read_bytes() == data
    capsys.readouterr()


def test_multi_source_containing_target_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """源文件已在目标目录中 → 目标即源自身；该文件被拒绝，其余文件仍继续复制。"""
    target_dir = tmp_path / "out"
    target_dir.mkdir()
    inside = write_file(target_dir / "a.txt", b"aaa")
    outside = write_file(tmp_path / "b.txt", b"bbb")

    assert cli.main([str(inside), str(outside), str(target_dir)]) == cli.EXIT_FAILURE

    assert inside.read_bytes() == b"aaa"
    assert (target_dir / "b.txt").read_bytes() == b"bbb"
    capsys.readouterr()


def test_recursive_onto_same_directory_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`safecopy -r dir dir`：拒绝执行，源树一个字节都不动。"""
    src_dir = tmp_path / "tree"
    write_file(src_dir / "a.txt", b"aaa")
    write_file(src_dir / "sub" / "b.bin", b"bbb")
    before = tree_snapshot(src_dir)
    digests = {
        rel: hashlib.sha256((src_dir / rel).read_bytes()).hexdigest()
        for rel in before
        if (src_dir / rel).is_file()
    }

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["-r", str(src_dir), str(src_dir)])
    assert excinfo.value.code == cli.EXIT_USAGE

    assert tree_snapshot(src_dir) == before
    for rel, digest in digests.items():
        assert hashlib.sha256((src_dir / rel).read_bytes()).hexdigest() == digest
    assert "内部" in capsys.readouterr().err


def test_recursive_target_inside_source_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """目标位于源目录内部（copy 进自身子目录）：拒绝且不产生任何写入。"""
    src_dir = tmp_path / "tree"
    write_file(src_dir / "a.txt", b"aaa")
    inner = src_dir / "copy"
    before = tree_snapshot(src_dir)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["-r", str(src_dir), str(inner)])
    assert excinfo.value.code == cli.EXIT_USAGE

    assert not inner.exists()
    assert tree_snapshot(src_dir) == before
    assert (src_dir / "a.txt").read_bytes() == b"aaa"
    capsys.readouterr()


def test_recursive_target_is_source_via_dotted_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-r dir dir/.` 这种写法也必须判定为同源。"""
    src_dir = tmp_path / "tree"
    write_file(src_dir / "a.txt", b"aaa")

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["-r", str(src_dir), str(src_dir / ".")])
    assert excinfo.value.code == cli.EXIT_USAGE

    assert (src_dir / "a.txt").read_bytes() == b"aaa"
    capsys.readouterr()


def test_recursive_target_outside_source_still_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """反向断言：目标在源之外（即使是源的兄弟目录）必须照常工作。"""
    src_dir = tmp_path / "tree"
    write_file(src_dir / "a.txt", b"aaa")
    dst_dir = tmp_path / "tree-copy"

    assert cli.main(["-r", str(src_dir), str(dst_dir)]) == cli.EXIT_OK

    assert (dst_dir / "a.txt").read_bytes() == b"aaa"
    capsys.readouterr()
