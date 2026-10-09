"""GUI 编排层单元测试（tasks 3.1 / 3.2）：预检、多源展开、逐文件容错、取消协议。

控制器不依赖 Qt，因此这些用例在没有任何 GUI 依赖的环境下也能运行，是
spec「Linux 宿主替代验收」中「业务逻辑可在无头环境测试」的直接证据。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from transfertools import cli, copier
from transfertools.gui import controller as gui_controller
from transfertools.gui.controller import (
    CopyController,
    CopyOptions,
    CopyPlan,
    CopySummary,
    ProgressListener,
    dedupe_paths,
    format_bytes,
    format_chunk_size,
    preflight_gui,
)
from transfertools.preflight import UsageError

MIB = copier.MIB


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class RecordingListener(ProgressListener):
    """记录全部回调，供断言调用序列与载荷。"""

    def __init__(self) -> None:
        self.plans: list[CopyPlan] = []
        self.file_starts: list[tuple[int, int, Path, Path]] = []
        self.file_progress: list[tuple[int, int]] = []
        self.progress: list[tuple[int, int]] = []
        self.logs: list[str] = []
        self.errors: list[tuple[Path, Path, str]] = []
        self.summaries: list[CopySummary] = []

    def on_plan(self, plan: CopyPlan) -> None:
        self.plans.append(plan)

    def on_file_start(self, index: int, total_files: int, src: Path, dst: Path) -> None:
        self.file_starts.append((index, total_files, src, dst))

    def on_file_progress(self, written: int, total: int) -> None:
        self.file_progress.append((written, total))

    def on_progress(self, done: int, total: int) -> None:
        self.progress.append((done, total))

    def on_log(self, message: str) -> None:
        self.logs.append(message)

    def on_file_error(self, src: Path, dst: Path, message: str) -> None:
        self.errors.append((src, dst, message))

    def on_summary(self, summary: CopySummary) -> None:
        self.summaries.append(summary)


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def listener() -> RecordingListener:
    return RecordingListener()


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    target = tmp_path / "out"
    target.mkdir()
    return target


# --------------------------------------------------------------------------- #
# 预检（spec「错误呈现」：参数性错误在开始前一次性报出）
# --------------------------------------------------------------------------- #


def test_preflight_rejects_empty_source_list(destination: Path) -> None:
    with pytest.raises(UsageError) as excinfo:
        preflight_gui([], destination, recursive=False)
    assert "源列表为空" in str(excinfo.value)


def test_preflight_rejects_missing_destination(tmp_path: Path) -> None:
    src = write_file(tmp_path / "a.txt", b"a")
    with pytest.raises(UsageError) as excinfo:
        preflight_gui([src], None, recursive=False)
    assert "尚未设置目标目录" in str(excinfo.value)


def test_preflight_rejects_missing_source(tmp_path: Path, destination: Path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(UsageError) as excinfo:
        preflight_gui([missing], destination, recursive=False)
    assert "源路径不存在" in str(excinfo.value)
    assert str(missing) in str(excinfo.value)


def test_preflight_rejects_directory_without_recursive(
    tmp_path: Path, destination: Path
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(UsageError) as excinfo:
        preflight_gui([folder], destination, recursive=False)
    assert "递归" in str(excinfo.value)


def test_preflight_rejects_destination_that_is_a_file(
    tmp_path: Path, listener: RecordingListener
) -> None:
    src = write_file(tmp_path / "a.txt", b"a")
    target = write_file(tmp_path / "not-a-dir", b"x")
    with pytest.raises(UsageError) as excinfo:
        preflight_gui([src], target, recursive=False)
    assert "不是目录" in str(excinfo.value)
    assert listener.errors == []


def test_preflight_rejects_nonexistent_destination_directory(
    tmp_path: Path,
) -> None:
    src = write_file(tmp_path / "a.txt", b"a")
    with pytest.raises(UsageError) as excinfo:
        preflight_gui([src], tmp_path / "missing-dir", recursive=False)
    assert "目标目录不存在" in str(excinfo.value)


def test_preflight_rejects_self_copy_for_directory_source(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    (source_dir / "nested").mkdir(parents=True)
    with pytest.raises(UsageError) as excinfo:
        preflight_gui([source_dir], source_dir / "nested", recursive=True)
    assert "位于源目录内部" in str(excinfo.value)


def test_preflight_messages_match_cli_semantics(tmp_path: Path) -> None:
    """GUI 与 CLI 共用同一套规则与文案（design D-g3）。"""
    missing = tmp_path / "nope.txt"
    folder = tmp_path / "folder"
    folder.mkdir()

    with pytest.raises(UsageError) as gui_missing:
        preflight_gui([missing], tmp_path, recursive=False)
    with pytest.raises(UsageError) as cli_missing:
        cli._preflight([missing], tmp_path, recursive=False)
    assert str(gui_missing.value) == str(cli_missing.value)

    with pytest.raises(UsageError) as gui_dir:
        preflight_gui([folder], tmp_path, recursive=False)
    with pytest.raises(UsageError) as cli_dir:
        cli._preflight([folder], tmp_path, recursive=False)
    assert str(gui_dir.value) == str(cli_dir.value)

    with pytest.raises(UsageError) as gui_self:
        preflight_gui([folder], folder / "inner", recursive=True)
    with pytest.raises(UsageError) as cli_self:
        cli._preflight([folder], folder / "inner", recursive=True)
    assert str(gui_self.value) == str(cli_self.value)


# --------------------------------------------------------------------------- #
# 计划展开（tasks 3.1）
# --------------------------------------------------------------------------- #


def test_build_plan_expands_directory_and_counts_bytes(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    source_dir = tmp_path / "tree"
    write_file(source_dir / "b.txt", b"b" * 10)
    write_file(source_dir / "a.txt", b"a" * 5)
    write_file(source_dir / "sub" / "c.txt", b"c" * 7)
    flat = write_file(tmp_path / "flat.bin", b"f" * 3)

    controller = CopyController(CopyOptions(recursive=True), listener)
    plan = controller.build_plan([source_dir, flat], destination)

    assert isinstance(plan, CopyPlan)
    assert plan.total_files == 4
    assert plan.bytes_known is True
    assert plan.total_bytes == 10 + 5 + 7 + 3
    assert [  # dst 相对路径逐分量比对，避免平台间路径分隔符差异（POSIX `/` vs Windows `\\`）
        (t.src.name, "/".join(t.dst.relative_to(destination).parts)) for t in plan.tasks
    ] == [
        ("a.txt", "a.txt"),
        ("b.txt", "b.txt"),
        ("c.txt", "sub/c.txt"),
        ("flat.bin", "flat.bin"),
    ]


def test_build_plan_reuses_copier_resolve_destination(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    src = write_file(tmp_path / "one.txt", b"1")
    controller = CopyController(CopyOptions(), listener)
    plan = controller.build_plan([src], destination)
    assert plan.tasks[0].dst == copier.resolve_destination(src, destination)
    assert plan.tasks[0].dst == destination / "one.txt"


def test_build_plan_degrades_when_size_unknown(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    """统计失败（如断链符号链接）→ bytes_known=False，进度降级为按文件数。"""
    source_dir = tmp_path / "tree"
    source_dir.mkdir()
    write_file(source_dir / "ok.txt", b"ok")
    (source_dir / "broken.link").symlink_to(source_dir / "does-not-exist")

    controller = CopyController(CopyOptions(recursive=True), listener)
    plan = controller.build_plan([source_dir], destination)

    assert plan.total_files == 2
    assert plan.bytes_known is False
    assert plan.total_bytes == 0

    summary = controller.run_plan(plan)

    assert summary.failed == 1, "断链源应在复制阶段报错"
    assert summary.succeeded == 1
    # 降级后总进度按文件数上报，量程等于文件总数。
    assert {total for _, total in listener.progress} == {2}


# --------------------------------------------------------------------------- #
# 执行（tasks 3.1：逐文件容错、进度上报）
# --------------------------------------------------------------------------- #


def test_run_copies_all_sources_and_reports(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    data_a = b"a" * (2 * MIB + 11)
    data_b = b"b" * 32
    src_a = write_file(tmp_path / "a.bin", data_a)
    src_b = write_file(tmp_path / "b.bin", data_b)

    controller = CopyController(CopyOptions(chunk_size=MIB), listener)
    summary = controller.run([src_a, src_b], destination)

    assert summary.succeeded == 2
    assert summary.failed == 0
    assert summary.cancelled is False
    assert summary.bytes_written == len(data_a) + len(data_b)
    assert (destination / "a.bin").read_bytes() == data_a
    assert (destination / "b.bin").read_bytes() == data_b
    assert summary.describe().startswith("全部完成")

    assert [index for index, _, _, _ in listener.file_starts] == [1, 2]
    assert [total for _, total, _, _ in listener.file_starts] == [2, 2]
    # 每个文件内部的字节进度按块递增，并在该文件结束时到达其总大小。
    assert listener.file_progress == [
        (MIB, len(data_a)),
        (2 * MIB, len(data_a)),
        (len(data_a), len(data_a)),
        (len(data_b), len(data_b)),
    ]
    # 总体进度量程为总字节数。
    assert {total for _, total in listener.progress} == {len(data_a) + len(data_b)}
    assert listener.progress[-1][0] == len(data_a) + len(data_b)
    # 日志包含阶段与结果（spec「复制执行与进度展示」）。
    assert any("写入中" in line for line in listener.logs)
    assert any("校验中" in line for line in listener.logs)
    assert any("完成:" in line for line in listener.logs)
    assert listener.errors == []
    assert listener.summaries[-1] is summary


def test_single_file_failure_does_not_stop_the_queue(
    tmp_path: Path, listener: RecordingListener
) -> None:
    """同源防护触发 → 该文件弹错误（回调）但其余文件继续（spec「错误呈现」）。"""
    destination = tmp_path / "out"
    destination.mkdir()
    same = write_file(destination / "same.txt", b"self")
    other = write_file(tmp_path / "other.txt", b"other")

    controller = CopyController(CopyOptions(), listener)
    summary = controller.run([same, other], destination)

    assert summary.total == 2
    assert summary.failed == 1
    assert summary.succeeded == 1
    assert [failure.src for failure in summary.failures] == [same]
    assert "同一文件" in summary.failures[0].message
    assert len(listener.errors) == 1
    assert listener.errors[0][0] == same
    assert (destination / "other.txt").read_bytes() == b"other"
    assert same.read_bytes() == b"self", "被拒绝复制时源文件必须零改动"


def test_no_clobber_skip_is_counted_as_failure(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    """与 CLI 一致：--no-clobber 跳过计入失败并逐条上报。"""
    src = write_file(tmp_path / "a.txt", b"new")
    existing = write_file(destination / "a.txt", b"old")

    controller = CopyController(CopyOptions(no_clobber=True), listener)
    summary = controller.run([src], destination)

    assert summary.failed == 1
    assert summary.skipped == 1
    assert summary.succeeded == 0
    assert existing.read_bytes() == b"old", "no-clobber 不得覆盖已存在目标"
    assert "跳过 1 个" in summary.describe()
    assert len(listener.errors) == 1


def test_options_are_forwarded_to_engine(
    tmp_path: Path, destination: Path, listener: RecordingListener,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """选项映射：chunk_size / preserve_metadata / 钩子参数确实传到了引擎。"""
    src = write_file(tmp_path / "a.txt", b"payload")
    captured: dict[str, object] = {}
    real_copy_file = copier.copy_file

    def spy(*args: object, **kwargs: object) -> copier.CopyResult:
        captured.update(kwargs)
        return real_copy_file(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(copier, "copy_file", spy)

    controller = CopyController(
        CopyOptions(chunk_size=MIB, preserve_metadata=False), listener
    )
    summary = controller.run([src], destination)

    assert summary.succeeded == 1
    assert captured["chunk_size"] == MIB
    assert captured["preserve_metadata"] is False
    assert callable(captured["on_bytes"])
    assert isinstance(captured["cancel_event"], threading.Event)
    assert callable(captured["warn"])
    assert callable(captured["progress"])


def test_recursive_run_recreates_directory_tree(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    source_dir = tmp_path / "tree"
    write_file(source_dir / "top.txt", b"top")
    write_file(source_dir / "nested" / "deep" / "bottom.txt", b"bottom")

    controller = CopyController(CopyOptions(recursive=True), listener)
    summary = controller.run([source_dir], destination)

    assert summary.succeeded == 2
    assert (destination / "top.txt").read_bytes() == b"top"
    assert (destination / "nested" / "deep" / "bottom.txt").read_bytes() == b"bottom"


# --------------------------------------------------------------------------- #
# 取消协议（tasks 3.2）
# --------------------------------------------------------------------------- #


def test_cancel_during_copy_removes_partial_and_marks_cancelled(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    first = write_file(tmp_path / "big.bin", b"x" * (3 * MIB))
    second = write_file(tmp_path / "second.bin", b"y" * 16)

    controller = CopyController(CopyOptions(chunk_size=MIB), listener)

    class CancellingListener(RecordingListener):
        def on_file_progress(self, written: int, total: int) -> None:
            super().on_file_progress(written, total)
            if written >= MIB:
                controller.request_cancel()

    cancelling = CancellingListener()
    controller.listener = cancelling

    summary = controller.run([first, second], destination)

    assert summary.cancelled is True
    assert summary.succeeded == 0
    assert summary.unfinished == 2, "取消时正在复制的文件与剩余文件都算未完成"
    assert not (destination / "big.bin").exists(), "半成品必须被引擎清理"
    assert not (destination / "second.bin").exists(), "取消后不得开始新文件"
    assert cancelling.errors == [], "取消不是错误：不应触发错误对话框回调"
    assert any("已取消" in line for line in cancelling.logs)
    assert cancelling.summaries[-1] is summary
    assert summary.describe().startswith("已取消")


def test_cancel_before_start_writes_nothing(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    src = write_file(tmp_path / "a.txt", b"a")

    controller = CopyController(CopyOptions(), listener)
    controller.request_cancel()
    assert controller.cancel_requested is True

    summary = controller.run([src], destination)

    assert summary.cancelled is True
    assert summary.succeeded == 0
    assert summary.failed == 0
    assert not (destination / "a.txt").exists()
    assert not listener.file_starts
    assert summary.unfinished == 1


def test_cancel_mid_verification_still_cleans_up(
    tmp_path: Path, destination: Path, listener: RecordingListener
) -> None:
    """校验阶段取消同样删除半成品（引擎在哈希读回循环里按块检查）。"""
    src = write_file(tmp_path / "a.bin", b"z" * (2 * MIB))
    controller = CopyController(CopyOptions(chunk_size=MIB), listener)

    original_stage = controller._stage

    def stage(stage_name: str) -> None:
        original_stage(stage_name)
        if stage_name == copier.STAGE_VERIFY:
            controller.request_cancel()

    controller._stage = stage  # type: ignore[method-assign]

    summary = controller.run([src], destination)

    assert summary.cancelled is True
    assert not (destination / "a.bin").exists()


# --------------------------------------------------------------------------- #
# 辅助函数与汇总文案
# --------------------------------------------------------------------------- #


def test_dedupe_paths_ignores_repeats_and_alias_writings(tmp_path: Path) -> None:
    real = write_file(tmp_path / "a.txt", b"a")
    second = write_file(tmp_path / "b.txt", b"b")

    result = dedupe_paths(
        [real, real, str(tmp_path / ".." / tmp_path.name / "a.txt"), second]
    )

    assert result == [real, second]


def test_dedupe_paths_resolves_relative_and_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "sub").mkdir()
    write_file(tmp_path / "sub" / "a.txt", b"a")
    monkeypatch.chdir(tmp_path)
    assert dedupe_paths(["sub/a.txt", "sub/../sub/a.txt"]) == [Path("sub/a.txt")]


def test_format_helpers() -> None:
    assert format_bytes(512) == "512 字节"
    assert format_bytes(2048) == "2.0 KiB"
    assert format_bytes(4 * MIB) == "4.0 MiB"
    assert format_chunk_size(4 * MIB) == "4 MiB"
    assert gui_controller.CHUNK_SIZE_CHOICES == (1, 2, 4, 8)


def test_summary_describe_variants() -> None:
    empty = CopySummary(0, 0, 0, False, 0)
    assert empty.describe() == "没有需要复制的文件。"

    partial = CopySummary(
        total=3,
        succeeded=2,
        failed=1,
        cancelled=True,
        bytes_written=10,
        failures=[],
    )
    assert partial.unfinished == 0
    assert partial.describe().startswith("已取消")

    failures = CopySummary(
        total=1, succeeded=0, failed=1, cancelled=False, bytes_written=0, skipped=1
    )
    assert "跳过 1" in failures.describe()
