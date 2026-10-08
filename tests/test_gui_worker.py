"""工作线程与信号桥接测试（tasks 4.1）。

验证 ``CopyWorker`` 在 ``QThread`` 中完成一次真实小文件复制，并且回调 → Qt 信号的
序列与载荷符合视图的期望；取消经 :meth:`CopyWorker.request_cancel` 直接调用即可生效。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from transfertools.gui.controller import CopyOptions
from transfertools.gui.worker import CopyWorker

MIB = 1024 * 1024


class WorkerHarness:
    """把 worker 放到 ``QThread`` 中跑起来，并记录全部信号（按发射顺序）。"""

    def __init__(self, qt_app: Any, sources: list[Path], destination: Path,
                 options: CopyOptions | None = None) -> None:
        from PySide6.QtCore import QThread

        self.worker = CopyWorker(sources, destination, options)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.events: list[tuple[str, Any]] = []
        self.summaries: list[Any] = []
        self.failures: list[str] = []
        self.worker.planReady.connect(lambda plan: self.events.append(("plan", plan)))
        self.worker.fileStarted.connect(
            lambda index, total, src, dst: self.events.append(("start", (index, total, src, dst)))
        )
        self.worker.fileProgress.connect(
            lambda written, total: self.events.append(("bytes", (written, total)))
        )
        self.worker.progressChanged.connect(
            lambda done, total: self.events.append(("total", (done, total)))
        )
        self.worker.logLine.connect(lambda line: self.events.append(("log", line)))
        self.worker.fileError.connect(
            lambda src, dst, message: self.events.append(("error", (src, dst, message)))
        )

        def on_summary(summary: Any) -> None:
            self.summaries.append(summary)
            self.events.append(("summary", summary))

        self.worker.summaryReady.connect(on_summary)

        def on_failed(message: str) -> None:
            self.failures.append(message)
            self.events.append(("failed", message))

        self.worker.failed.connect(on_failed)
        self.worker.finished.connect(lambda: self.events.append(("finished", None)))
        self.qt_app = qt_app

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.thread.quit()
        self.thread.wait(5000)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


@pytest.fixture
def harness_factory(qt_app: Any) -> Any:
    created: list[WorkerHarness] = []

    def factory(sources: list[Path], destination: Path,
                options: CopyOptions | None = None) -> WorkerHarness:
        harness = WorkerHarness(qt_app, sources, destination, options)
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        harness.stop()


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_worker_copies_file_and_emits_expected_signal_sequence(
    tmp_path: Path, harness_factory: Any, wait_for: Any
) -> None:
    data = b"payload" * 100
    src = write_file(tmp_path / "src.bin", data)
    destination = tmp_path / "out"
    destination.mkdir()

    harness = harness_factory([src], destination, CopyOptions(chunk_size=MIB))
    harness.start()
    assert wait_for(lambda: "finished" in harness.kinds())

    kinds = harness.kinds()
    assert kinds[0] == "plan", "第一个信号应是 planReady"
    assert kinds[-1] == "finished"
    assert "start" in kinds and "bytes" in kinds and "total" in kinds and "log" in kinds
    assert kinds.index("summary") < kinds.index("finished")
    assert "failed" not in kinds and "error" not in kinds

    plan = harness.events[0][1]
    assert plan.total_files == 1 and plan.total_bytes == len(data)
    summary = harness.summaries[0]
    assert summary.succeeded == 1 and summary.failed == 0
    assert (destination / "src.bin").read_bytes() == data

    start_payload = next(payload for kind, payload in harness.events if kind == "start")
    assert start_payload[0] == 1 and start_payload[1] == 1
    assert start_payload[2] == str(src)
    assert start_payload[3] == str(destination / "src.bin")
    bytes_payloads = [payload for kind, payload in harness.events if kind == "bytes"]
    assert bytes_payloads[-1] == (len(data), len(data))
    total_payloads = [payload for kind, payload in harness.events if kind == "total"]
    assert total_payloads[0] == (0, len(data))
    assert total_payloads[-1] == (len(data), len(data))


def test_worker_reports_preflight_failure_without_summary(
    tmp_path: Path, harness_factory: Any, wait_for: Any
) -> None:
    destination = tmp_path / "out"
    destination.mkdir()

    harness = harness_factory([tmp_path / "missing.txt"], destination)
    harness.start()
    assert wait_for(lambda: "finished" in harness.kinds())

    kinds = harness.kinds()
    assert "failed" in kinds and "summary" not in kinds
    assert "源路径不存在" in harness.failures[0]
    assert not list(destination.iterdir())


def test_worker_cancel_from_gui_thread_stops_copy(
    tmp_path: Path, harness_factory: Any, wait_for: Any
) -> None:
    src = write_file(tmp_path / "big.bin", b"x" * (4 * MIB))
    destination = tmp_path / "out"
    destination.mkdir()

    harness = harness_factory([src], destination, CopyOptions(chunk_size=MIB))
    harness.start()

    # 等到第一个字节进度信号，再从「GUI 线程」直接请求取消。
    assert wait_for(lambda: any(kind == "bytes" for kind in harness.kinds()))
    harness.worker.request_cancel()
    assert harness.worker.cancel_requested is True
    assert wait_for(lambda: "finished" in harness.kinds())

    summary = harness.summaries[0]
    assert summary.cancelled is True
    assert summary.succeeded == 0
    assert not (destination / "big.bin").exists(), "半成品必须被清理"
    assert "error" not in harness.kinds(), "取消不应触发错误信号"
    assert harness.failures == []
