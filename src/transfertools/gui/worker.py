"""工作线程与信号桥接（design.md D-g3、tasks 4.1）。

复制在 :class:`~PySide6.QtCore.QThread` 中执行：:class:`CopyWorker` 是 ``QObject``，
内部持有不依赖 Qt 的 :class:`~transfertools.gui.controller.CopyController`，并把控制器的
回调经「桥接对象」发射为 Qt 信号（跨线程自动排队到 GUI 线程），因此界面不会被复制阻塞。

取消：**必须直接调用** :meth:`CopyWorker.request_cancel`（普通 Python 调用）。工作线程在
``run()`` 执行期间不处理自身事件循环，若用信号/排队槽转发取消，要等复制结束才生效。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ..preflight import UsageError
from .controller import (
    CopyController,
    CopyOptions,
    CopyPlan,
    CopySummary,
    ProgressListener,
)

__all__ = ["CopyWorker"]


class _SignalBridge(ProgressListener):
    """控制器回调 → Qt 信号（由工作线程发射，Qt 自动排队到 GUI 线程）。"""

    def __init__(self, worker: "CopyWorker") -> None:
        self._worker = worker

    def on_plan(self, plan: CopyPlan) -> None:
        self._worker.planReady.emit(plan)

    def on_file_start(self, index: int, total_files: int, src: Path, dst: Path) -> None:
        self._worker.fileStarted.emit(index, total_files, str(src), str(dst))

    def on_file_progress(self, written: int, total: int) -> None:
        self._worker.fileProgress.emit(written, total)

    def on_progress(self, done: int, total: int) -> None:
        self._worker.progressChanged.emit(done, total)

    def on_log(self, message: str) -> None:
        self._worker.logLine.emit(message)

    def on_file_error(self, src: Path, dst: Path, message: str) -> None:
        self._worker.fileError.emit(str(src), str(dst), message)

    def on_summary(self, summary: CopySummary) -> None:
        self._worker.summaryReady.emit(summary)


class CopyWorker(QObject):
    """在 ``QThread`` 中运行一次复制，并把进度/错误/汇总发射为信号。"""

    #: 预检通过、开始执行前（载荷为 :class:`CopyPlan`）。
    planReady = Signal(object)
    #: ``(序号, 总数, 源, 目标)``：某个文件开始复制。
    fileStarted = Signal(int, int, str, str)
    #: ``(当前文件已写入字节, 当前文件总字节)``。
    fileProgress = Signal(int, int)
    #: ``(总体进度, 总量)``；单位为字节（可统计时）或文件数。
    progressChanged = Signal(int, int)
    #: 一行中文日志。
    logLine = Signal(str)
    #: ``(源, 目标, 原因)``：单文件失败（不中断其余文件）。
    fileError = Signal(str, str, str)
    #: 执行结束的汇总（载荷为 :class:`CopySummary`）。
    summaryReady = Signal(object)
    #: 无法开始（预检失败、未预期错误）：载荷为中文说明。
    failed = Signal(str)
    #: 工作线程的任务已全部返回（无论成功、失败或取消）。
    finished = Signal()

    def __init__(
        self,
        sources: Iterable[str | Path],
        destination: str | Path,
        options: CopyOptions | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.sources = [Path(item) for item in sources]
        self.destination = Path(destination)
        self.controller = CopyController(options or CopyOptions(), _SignalBridge(self))

    @Slot()
    def run(self) -> None:
        """线程体：预检 + 逐文件复制（阻塞），结束时必定发射 :attr:`finished`。"""
        try:
            try:
                plan = self.controller.build_plan(self.sources, self.destination)
            except UsageError as exc:
                self.failed.emit(str(exc))
                return
            except OSError as exc:
                self.failed.emit(f"读取源列表失败: {exc}")
                return
            self.controller.run_plan(plan)
        except Exception as exc:  # noqa: BLE001 - 兜底：工作线程异常不能静默丢失
            self.failed.emit(f"复制过程中出现未预期的错误: {exc}")
        finally:
            self.finished.emit()

    def request_cancel(self) -> None:
        """请求取消（线程安全，供 GUI 线程直接调用）：引擎在块边界响应。"""
        self.controller.request_cancel()

    @property
    def cancel_requested(self) -> bool:
        return self.controller.cancel_requested
