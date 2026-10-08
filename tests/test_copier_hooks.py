"""GUI 复用钩子（design D-g3，tasks 1.1）单元测试：字节进度回调与协作式取消。

覆盖点：

- ``on_bytes(written, total)`` 每写完一块回调一次，末次等于文件总字节数；
- ``cancel_event`` 置位后在块边界抛 :class:`CopyCancelledError`（``SafeCopyError`` 子类），
  半成品走既有清理路径被删除、源文件零改动；
- 校验读回阶段（``verify_digests``）同样按块响应取消；
- 两个钩子都不传时，``copy_file`` 行为与变更前一致（既有 110 项测试之外的补充断言）。
"""

from __future__ import annotations

import hashlib
import inspect
import random
import threading
from pathlib import Path

import pytest

from transfertools import copier

MIB = copier.MIB


def make_data(size: int, seed: int = 20261008) -> bytes:
    return random.Random(seed).randbytes(size)


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# 向后兼容：签名为可选关键字参数、默认值下行为不变
# --------------------------------------------------------------------------- #


def test_hook_parameters_are_optional_keywords() -> None:
    signature = inspect.signature(copier.copy_file)
    for name in ("on_bytes", "cancel_event"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None
    assert inspect.signature(copier.sha256_file).parameters["cancel_event"].default is None
    assert inspect.signature(copier.verify_digests).parameters["cancel_event"].default is None


def test_cancelled_error_is_safecopy_error() -> None:
    assert issubclass(copier.CopyCancelledError, copier.SafeCopyError)
    assert "CopyCancelledError" in copier.__all__


def test_without_hooks_behaves_as_before(tmp_path: Path) -> None:
    """不传 on_bytes / cancel_event 时结果与变更前一致（size / sha256 / 阶段序列）。"""
    data = make_data(3 * MIB + 123)
    src = write_file(tmp_path / "src.bin", data)
    dst = tmp_path / "dst.bin"
    stages: list[str] = []

    result = copier.copy_file(
        src, dst, chunk_size=MIB, progress=stages.append, preallocate=False
    )

    assert stages == [copier.STAGE_WRITE, copier.STAGE_VERIFY, copier.STAGE_DONE]
    assert result.src == src and result.dst == dst
    assert result.size == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.chunk_size == MIB
    assert dst.read_bytes() == data


# --------------------------------------------------------------------------- #
# 字节进度回调
# --------------------------------------------------------------------------- #


def test_on_bytes_reports_each_chunk_and_final_total(tmp_path: Path) -> None:
    size = 3 * MIB + 4096
    src = write_file(tmp_path / "src.bin", make_data(size))
    dst = tmp_path / "dst.bin"
    seen: list[tuple[int, int]] = []

    copier.copy_file(
        src, dst, chunk_size=MIB, on_bytes=lambda written, total: seen.append((written, total))
    )

    assert seen == [(MIB, size), (2 * MIB, size), (3 * MIB, size), (size, size)]
    assert [written for written, _ in seen] == sorted(written for written, _ in seen)
    assert dst.exists()


def test_on_bytes_not_called_without_callback(tmp_path: Path) -> None:
    """默认 None 时不应有任何额外回调开销（用 spy 的 warn 证明路径一致）。"""
    src = write_file(tmp_path / "src.bin", make_data(4096))
    warnings: list[str] = []
    result = copier.copy_file(
        src, tmp_path / "dst.bin", warn=warnings.append, preallocate=False
    )
    assert warnings == []
    assert result.size == 4096


# --------------------------------------------------------------------------- #
# 协作式取消：写入阶段
# --------------------------------------------------------------------------- #


def test_cancel_during_write_removes_partial_and_keeps_source(tmp_path: Path) -> None:
    size = 4 * MIB
    data = make_data(size)
    src = write_file(tmp_path / "src.bin", data)
    dst = tmp_path / "dst.bin"
    cancel_event = threading.Event()
    written_at_cancel: list[int] = []

    def on_bytes(written: int, total: int) -> None:
        if written >= 2 * MIB:
            written_at_cancel.append(written)
            cancel_event.set()

    with pytest.raises(copier.CopyCancelledError) as excinfo:
        copier.copy_file(
            src,
            dst,
            chunk_size=MIB,
            on_bytes=on_bytes,
            cancel_event=cancel_event,
            preallocate=False,
        )

    assert "取消" in str(excinfo.value)
    assert written_at_cancel == [2 * MIB]
    assert not dst.exists(), "取消后引擎必须删除未校验的半成品文件"
    assert src.read_bytes() == data, "源文件必须零改动"


def test_cancel_before_start_creates_no_leftover(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(1024))
    dst = tmp_path / "dst.bin"
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(copier.CopyCancelledError):
        copier.copy_file(src, dst, cancel_event=cancel_event)

    assert not dst.exists()


def test_cancel_removes_preexisting_destination_like_other_failures(tmp_path: Path) -> None:
    """取消走既有 D4 清理语义：已存在的同名目标已被截断，同样在失败清理中删除。"""
    src = write_file(tmp_path / "src.bin", make_data(2048))
    dst = write_file(tmp_path / "dst.bin", b"old-content")
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(copier.CopyCancelledError):
        copier.copy_file(src, dst, cancel_event=cancel_event)

    assert not dst.exists()


# --------------------------------------------------------------------------- #
# 协作式取消：校验读回阶段
# --------------------------------------------------------------------------- #


def test_cancel_during_verification_removes_partial(tmp_path: Path) -> None:
    data = make_data(2 * MIB)
    src = write_file(tmp_path / "src.bin", data)
    dst = tmp_path / "dst.bin"
    cancel_event = threading.Event()

    def progress(stage: str) -> None:
        if stage == copier.STAGE_VERIFY:
            # 写入阶段已通过；校验读回的第一块边界即应响应取消。
            cancel_event.set()

    with pytest.raises(copier.CopyCancelledError):
        copier.copy_file(
            src, dst, chunk_size=MIB, progress=progress, cancel_event=cancel_event
        )

    assert not dst.exists()


def test_sha256_file_checks_cancel_each_chunk(tmp_path: Path) -> None:
    src = write_file(tmp_path / "src.bin", make_data(2 * MIB + 1))
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(copier.CopyCancelledError):
        copier.sha256_file(src, MIB, cancel_event=cancel_event)

    assert copier.sha256_file(src, MIB) == hashlib.sha256(src.read_bytes()).hexdigest()
