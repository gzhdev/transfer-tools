"""主窗口：单窗口三段式中文界面 + 状态机（design.md D-g2、tasks 5.1 / 5.2）。

三段式布局（自上而下）：

1. **源列表区**：``QListWidget``（接受拖放）+「添加文件…」「添加文件夹…」「移除选中」「清空」；
2. **目标与选项区**：目标目录输入 + 浏览，递归开关、块大小 1/2/4/8 MiB（默认 4）、
   no-clobber、no-preserve-metadata；
3. **进度与日志区**：总进度条、当前文件标签、只读日志、「开始」「取消」。

状态机：``idle → running → idle``。``running`` 期间锁定全部选项控件与源列表改动，仅
「取消」可用（日志区保持可滚动）；结束后恢复可编辑。

本模块只负责控件装配、状态切换与对话框呈现，复制逻辑全部在
:mod:`transfertools.gui.controller`（不依赖 Qt），因此可在 ``QT_QPA_PLATFORM=offscreen``
下无头测试。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import copier
from ..preflight import UsageError
from .controller import (
    CHUNK_SIZE_CHOICES,
    CopyOptions,
    CopyPlan,
    CopySummary,
    dedupe_paths,
    format_bytes,
    format_chunk_size,
    preflight_gui,
)
from .worker import CopyWorker

__all__ = [
    "STATE_IDLE",
    "STATE_RUNNING",
    "SourceListWidget",
    "MainWindow",
    "format_error_dialog",
    "format_summary_dialog",
]

#: 空闲：可编辑源列表与选项。
STATE_IDLE = "idle"
#: 复制进行中：选项锁定，仅「取消」可用。
STATE_RUNNING = "running"

#: 汇总对话框中最多逐条列出的失败条目数（完整列表在日志区）。
MAX_DIALOG_FAILURES = 10


def format_error_dialog(src: str, dst: str, message: str) -> str:
    """单文件失败对话框正文（spec「错误呈现」：同时给出文件、原因与后续行为）。"""
    return (
        f"文件: {src}\n目标: {dst}\n原因: {message}\n\n"
        "该文件的目标半成品已由引擎清理；其余文件将继续复制（与命令行语义一致）。"
    )


def format_summary_dialog(summary: CopySummary) -> str:
    """复制结束对话框正文；存在失败时逐条列出（超出上限的部分引导到日志区）。"""
    lines = [summary.describe()]
    if summary.failures:
        lines.append("")
        lines.append("失败条目:")
        for failure in summary.failures[:MAX_DIALOG_FAILURES]:
            lines.append(f"  · {failure.src} -> {failure.dst}：{failure.message}")
        remaining = len(summary.failures) - MAX_DIALOG_FAILURES
        if remaining > 0:
            lines.append(f"  …… 其余 {remaining} 条详见日志区")
    return "\n".join(lines)


class SourceListWidget(QListWidget):
    """源列表控件：接受 OS 级文件/文件夹拖放，并把路径交给窗口去重（tasks 5.1）。"""

    #: 拖放得到的本地路径列表（窗口负责去重、校验与状态判断）。
    pathsDropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setToolTip("可直接把文件或文件夹拖到这里")

    @staticmethod
    def paths_from_mime(mime: object) -> list[str]:
        """从拖放数据中提取本地路径（忽略非本地 URL）。"""
        urls = mime.urls()  # type: ignore[attr-defined]
        return [
            url.toLocalFile()
            for url in urls
            if url.isLocalFile() and url.toLocalFile()
        ]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self.paths_from_mime(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.pathsDropped.emit(paths)


class MainWindow(QMainWindow):
    """safecopy 图形界面主窗口。"""

    APP_TITLE = "safecopy 图形界面 — 分块写入式安全文件复制"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.APP_TITLE)
        self.resize(900, 760)

        #: 状态机当前状态（``idle`` / ``running``）。
        self.state = STATE_IDLE
        #: 最近一次执行的汇总（未执行过为 ``None``）。
        self.summary: CopySummary | None = None

        self._worker: CopyWorker | None = None
        self._thread: QThread | None = None
        self._plan: CopyPlan | None = None
        self._cancel_requested = False
        self._current_index = 0
        self._current_total = 0
        self._current_src = ""

        self._build_ui()
        self._connect_signals()
        self._update_controls()

    # ------------------------------------------------------------------ #
    # 界面装配（D-g2 三段式）
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        layout.addWidget(self._build_source_group())
        layout.addWidget(self._build_option_group())
        layout.addWidget(self._build_progress_group(), stretch=1)

    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox("① 源文件 / 文件夹", self)
        box = QVBoxLayout(group)

        self.source_list = SourceListWidget(group)
        box.addWidget(self.source_list, stretch=1)

        row = QHBoxLayout()
        self.add_files_button = QPushButton("添加文件…", group)
        self.add_folder_button = QPushButton("添加文件夹…", group)
        self.remove_button = QPushButton("移除选中", group)
        self.clear_button = QPushButton("清空", group)
        for button in (
            self.add_files_button,
            self.add_folder_button,
            self.remove_button,
            self.clear_button,
        ):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)

        self.source_hint_label = QLabel(
            "提示：点击「添加文件…」可多选，也可把文件或文件夹直接拖入上方列表。", group
        )
        self.source_hint_label.setWordWrap(True)
        box.addWidget(self.source_hint_label)
        return group

    def _build_option_group(self) -> QGroupBox:
        group = QGroupBox("② 目标目录与复制选项", self)
        box = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel("目标目录:", group))
        self.destination_edit = QLineEdit(group)
        self.destination_edit.setPlaceholderText("选择或输入目标目录（必须已存在）")
        self.browse_button = QPushButton("浏览…", group)
        row.addWidget(self.destination_edit, stretch=1)
        row.addWidget(self.browse_button)
        box.addLayout(row)

        options_row = QHBoxLayout()
        self.recursive_check = QCheckBox("递归复制目录", group)
        self.recursive_check.setToolTip("源列表包含目录时必须勾选（等价于 CLI 的 -r）")
        options_row.addWidget(self.recursive_check)

        options_row.addWidget(QLabel("块大小:", group))
        self.chunk_combo = QComboBox(group)
        for mib in CHUNK_SIZE_CHOICES:
            self.chunk_combo.addItem(format_chunk_size(mib * copier.MIB), mib * copier.MIB)
        self.chunk_combo.setCurrentIndex(
            CHUNK_SIZE_CHOICES.index(copier.DEFAULT_CHUNK_SIZE // copier.MIB)
        )
        self.chunk_combo.setToolTip("分块写入的块大小，范围 1~8 MiB，默认 4 MiB")
        options_row.addWidget(self.chunk_combo)

        self.no_clobber_check = QCheckBox("目标已存在时跳过", group)
        self.no_clobber_check.setToolTip("等价于 CLI 的 --no-clobber；跳过计为失败")
        options_row.addWidget(self.no_clobber_check)

        self.no_preserve_check = QCheckBox("不保留权限与时间戳", group)
        self.no_preserve_check.setToolTip("等价于 CLI 的 --no-preserve-metadata")
        options_row.addWidget(self.no_preserve_check)

        options_row.addStretch(1)
        box.addLayout(options_row)
        return group

    def _build_progress_group(self) -> QGroupBox:
        group = QGroupBox("③ 进度与日志", self)
        box = QVBoxLayout(group)

        self.progress_bar = QProgressBar(group)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        box.addWidget(self.progress_bar)

        self.current_file_label = QLabel("尚未开始。", group)
        self.current_file_label.setWordWrap(True)
        box.addWidget(self.current_file_label)

        self.log_view = QPlainTextEdit(group)
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("复制日志将显示在这里")
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        box.addWidget(self.log_view, stretch=1)

        row = QHBoxLayout()
        self.start_button = QPushButton("开始", group)
        self.start_button.setDefault(True)
        self.cancel_button = QPushButton("取消", group)
        row.addWidget(self.start_button)
        row.addWidget(self.cancel_button)
        self.status_label = QLabel("状态：就绪", group)
        row.addWidget(self.status_label, stretch=1)
        box.addLayout(row)
        return group

    def _connect_signals(self) -> None:
        self.add_files_button.clicked.connect(self.choose_files)
        self.add_folder_button.clicked.connect(self.choose_folder)
        self.remove_button.clicked.connect(self.remove_selected)
        self.clear_button.clicked.connect(self.clear_sources)
        self.browse_button.clicked.connect(self.choose_destination)
        self.destination_edit.textChanged.connect(self._update_controls)
        self.source_list.pathsDropped.connect(self.add_sources)
        self.start_button.clicked.connect(self.start_copy)
        self.cancel_button.clicked.connect(self.request_cancel)

    # ------------------------------------------------------------------ #
    # 源列表维护（去重、增删）
    # ------------------------------------------------------------------ #

    def source_paths(self) -> list[Path]:
        """当前源列表内容（按加入顺序）。"""
        return [
            Path(self.source_list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.source_list.count())
        ]

    def add_sources(self, paths: Iterable[str | Path]) -> int:
        """添加源路径并去重，返回实际新增的条目数（spec「源文件收集」）。

        ``running`` 期间拒绝改动（与「复制期间锁定输入」一致）。
        """
        if self.state == STATE_RUNNING:
            self.append_log("复制进行中，暂不能修改源列表。")
            return 0

        candidates = dedupe_paths([str(item) for item in paths if str(item)])
        existing = {copier.canonical_path(item) for item in self.source_paths()}
        added = 0
        for path in candidates:
            key = copier.canonical_path(path)
            if key in existing:
                continue
            item = QListWidgetItem(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            self.source_list.addItem(item)
            existing.add(key)
            added += 1
        if added:
            self.append_log(f"已添加 {added} 个源（共 {self.source_list.count()} 个）。")
        self._update_controls()
        return added

    def remove_selected(self) -> int:
        """移除选中的源条目，返回移除数量。"""
        if self.state == STATE_RUNNING:
            self.append_log("复制进行中，暂不能修改源列表。")
            return 0
        rows = sorted(
            (self.source_list.row(item) for item in self.source_list.selectedItems()),
            reverse=True,
        )
        for row in rows:
            self.source_list.takeItem(row)
        if rows:
            self.append_log(f"已移除 {len(rows)} 个源（共 {self.source_list.count()} 个）。")
        self._update_controls()
        return len(rows)

    def clear_sources(self) -> None:
        """清空源列表。"""
        if self.state == STATE_RUNNING:
            self.append_log("复制进行中，暂不能修改源列表。")
            return
        self.source_list.clear()
        self.append_log("已清空源列表。")
        self._update_controls()

    def choose_files(self) -> int:
        """「添加文件…」：多选文件对话框。"""
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要复制的文件")
        return self.add_sources(paths) if paths else 0

    def choose_folder(self) -> int:
        """「添加文件夹…」：目录对话框。"""
        folder = QFileDialog.getExistingDirectory(self, "选择要复制的文件夹")
        return self.add_sources([folder]) if folder else 0

    def choose_destination(self) -> None:
        """「浏览…」：目标目录选择。"""
        folder = QFileDialog.getExistingDirectory(
            self, "选择目标目录", self.destination_edit.text().strip()
        )
        if folder:
            self.destination_edit.setText(folder)

    # ------------------------------------------------------------------ #
    # 选项映射与状态机
    # ------------------------------------------------------------------ #

    def destination_path(self) -> Path | None:
        """目标目录（未填写时为 ``None``）。"""
        text = self.destination_edit.text().strip()
        return Path(text).expanduser() if text else None

    def options(self) -> CopyOptions:
        """控件 → 引擎选项映射（与 CLI 选项一一对应）。"""
        chunk_size = self.chunk_combo.currentData()
        return CopyOptions(
            recursive=self.recursive_check.isChecked(),
            chunk_size=int(chunk_size)
            if chunk_size is not None
            else copier.DEFAULT_CHUNK_SIZE,
            no_clobber=self.no_clobber_check.isChecked(),
            preserve_metadata=not self.no_preserve_check.isChecked(),
        )

    def set_state(self, state: str) -> None:
        """切换状态机并同步控件可用性。"""
        self.state = state
        self._update_controls()

    def _update_controls(self) -> None:
        running = self.state == STATE_RUNNING
        has_sources = self.source_list.count() > 0
        has_destination = bool(self.destination_edit.text().strip())

        self.start_button.setEnabled(not running and has_sources and has_destination)
        self.cancel_button.setEnabled(running and not self._cancel_requested)

        for widget in (
            self.add_files_button,
            self.add_folder_button,
            self.remove_button,
            self.clear_button,
            self.destination_edit,
            self.browse_button,
            self.recursive_check,
            self.chunk_combo,
            self.no_clobber_check,
            self.no_preserve_check,
        ):
            widget.setEnabled(not running)
        # 源列表保持可见可滚动，但复制期间不接受拖放投放。
        self.source_list.setAcceptDrops(not running)

        if running:
            self.status_label.setText("状态：复制中…")
        elif self.summary is None:
            self.status_label.setText("状态：就绪")

    def append_log(self, message: str) -> None:
        """向日志区追加一行并滚到底部。"""
        self.log_view.appendPlainText(message)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ------------------------------------------------------------------ #
    # 执行与取消
    # ------------------------------------------------------------------ #

    def start_copy(self) -> None:
        """「开始」：预检 → 起工作线程 → ``running``。"""
        if self.state == STATE_RUNNING:
            return

        sources = self.source_paths()
        destination = self.destination_path()
        options = self.options()
        try:
            preflight_gui(sources, destination, recursive=options.recursive)
        except UsageError as exc:
            self.append_log(f"无法开始: {exc}")
            self.show_warning_dialog("无法开始", f"{exc}\n\n未产生任何目标文件。")
            self._update_controls()
            return

        assert destination is not None  # preflight_gui 已保证
        self.summary = None
        self._cancel_requested = False
        self.cancel_button.setText("取消")
        self.progress_bar.setValue(0)
        self.current_file_label.setText("正在准备…")
        self.append_log(
            f"开始复制：{len(sources)} 个源 → {destination}"
            f"（块大小 {format_chunk_size(options.chunk_size)}"
            f"；递归 {'开' if options.recursive else '关'}"
            f"；跳过已存在 {'开' if options.no_clobber else '关'}）"
        )
        self.set_state(STATE_RUNNING)
        self._start_worker(sources, destination, options)

    def _start_worker(
        self, sources: list[Path], destination: Path, options: CopyOptions
    ) -> None:
        thread = QThread(self)
        worker = CopyWorker(sources, destination, options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.planReady.connect(self._on_plan)
        worker.fileStarted.connect(self._on_file_started)
        worker.fileProgress.connect(self._on_file_progress)
        worker.progressChanged.connect(self._on_progress)
        worker.logLine.connect(self.append_log)
        worker.fileError.connect(self._on_file_error)
        worker.summaryReady.connect(self._on_summary)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self._thread = thread
        self._worker = worker
        thread.start()

    def request_cancel(self) -> None:
        """「取消」：置位引擎取消事件（直接调用，见 worker.py 说明）。"""
        if self.state != STATE_RUNNING or self._worker is None:
            return
        self._worker.request_cancel()
        self._cancel_requested = True
        self.cancel_button.setText("正在取消…")
        self.cancel_button.setEnabled(False)
        self.current_file_label.setText("正在取消…（当前文件将在块边界停止并删除半成品）")
        self.append_log(
            "已请求取消：当前文件将在块边界停止、半成品被删除，剩余文件不再处理。"
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        """关窗前确保工作线程停止，避免 ``QThread`` 在运行中被销毁（崩溃）。"""
        if self.state == STATE_RUNNING and self._worker is not None:
            self._worker.request_cancel()
            thread = self._thread
            if thread is not None:
                thread.quit()
                thread.wait(5000)
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    # 工作线程信号处理（均在 GUI 线程执行）
    # ------------------------------------------------------------------ #

    @Slot(object)
    def _on_plan(self, plan: CopyPlan) -> None:
        self._plan = plan
        self.progress_bar.setValue(0)
        if plan.total_files == 0:
            self.append_log("预检通过：没有发现需要复制的文件。")

    @Slot(int, int, str, str)
    def _on_file_started(self, index: int, total: int, src: str, dst: str) -> None:
        self._current_index = index
        self._current_total = total
        self._current_src = src
        self.current_file_label.setText(f"正在复制 [{index}/{total}]：{src}")

    @Slot(int, int)
    def _on_file_progress(self, written: int, total: int) -> None:
        self.current_file_label.setText(
            f"正在复制 [{self._current_index}/{self._current_total}] {self._current_src}"
            f"：{format_bytes(written)} / {format_bytes(total)}"
        )

    @Slot(int, int)
    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.progress_bar.setValue(0)
            return
        # 用千分比而非字节数作为量程，避免 >2 GiB 的文件超出 QProgressBar 的 int 范围。
        self.progress_bar.setValue(max(0, min(1000, int(done * 1000 / total))))

    @Slot(str, str, str)
    def _on_file_error(self, src: str, dst: str, message: str) -> None:
        """单文件失败：模态对话框 + 日志（日志已由控制器写入），不中断队列。"""
        self.show_error_dialog(src, dst, message)

    @Slot(object)
    def _on_summary(self, summary: CopySummary) -> None:
        self.summary = summary
        if summary.cancelled:
            self.current_file_label.setText("已取消。")
        elif summary.total and summary.failed == 0:
            self.progress_bar.setValue(1000)
            self.current_file_label.setText(f"已完成 {summary.succeeded} 个文件。")
        else:
            self.current_file_label.setText(
                f"已结束：成功 {summary.succeeded} 个，失败 {summary.failed} 个。"
            )
        self.status_label.setText(f"状态：{summary.describe()}")
        self.show_summary_dialog(summary)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        """无法开始（预检失败或未预期错误）。"""
        self.append_log(f"无法开始: {message}")
        self.show_warning_dialog("无法开始", message)

    @Slot()
    def _on_finished(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
        self._thread = None
        self._worker = None
        self._cancel_requested = False
        self.cancel_button.setText("取消")
        self.set_state(STATE_IDLE)

    # ------------------------------------------------------------------ #
    # 对话框（测试可覆写，避免无头环境阻塞）
    # ------------------------------------------------------------------ #

    def show_error_dialog(self, src: str, dst: str, message: str) -> None:
        """复制错误：模态对话框（spec「错误呈现」双通道之一）。"""
        QMessageBox.critical(self, "复制失败", format_error_dialog(src, dst, message))

    def show_warning_dialog(self, title: str, message: str) -> None:
        """参数性错误提示。"""
        QMessageBox.warning(self, title, message)

    def show_summary_dialog(self, summary: CopySummary) -> None:
        """结束提示：全部成功 → 成功提示；存在失败 → 失败提示并列出条目；取消不弹窗。"""
        if summary.cancelled:
            return
        if summary.total == 0:
            QMessageBox.information(
                self, "没有需要复制的文件", "选定的源中没有可复制的普通文件。"
            )
            return
        if summary.failed == 0:
            QMessageBox.information(
                self,
                "复制完成",
                f"成功复制 {summary.succeeded} 个文件，"
                f"共 {format_bytes(summary.bytes_written)}。\n"
                "所有文件均已通过 SHA-256 完整性校验。",
            )
            return
        QMessageBox.warning(self, "复制完成但存在失败", format_summary_dialog(summary))
