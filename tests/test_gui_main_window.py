"""主窗口 UI 测试（tasks 5.1 / 5.2、design D-g5）：``QT_QPA_PLATFORM=offscreen``。

全部用例都驱动**真实控件**与**真实复制**（临时目录里的小文件），并按 spec 的 8 项
需求逐条核对：三段式中文布局、源收集与拖拽去重、选项配置与运行期锁定、进度与日志、
错误双通道呈现、取消与半成品清理、可再次开始等。

对话框默认由 :class:`DialogRecorder` 接管以避免无头环境阻塞；另有独立用例通过
monkeypatch ``QMessageBox`` 静态方法验证真实对话框代码路径与文案。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
)

from transfertools import copier
from transfertools.gui.controller import CHUNK_SIZE_CHOICES, format_bytes
from transfertools.gui.main_window import (
    STATE_IDLE,
    STATE_RUNNING,
    MainWindow,
    format_error_dialog,
    format_summary_dialog,
)

MIB = copier.MIB
CJK = re.compile(r"[\u4e00-\u9fff]")


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class InjectedDrop:
    """构造一次真实拖放事件并投递到控件（「拖拽事件注入」）。

    注意：``QDropEvent`` 不持有 ``QMimeData`` 的所有权，Python 侧必须一直引用它，
    否则 ``event.mimeData()`` 会读到已回收的对象（实测为段错误），因此由本对象持有。
    """

    def __init__(self, paths: list[Path]) -> None:
        self.mime = QMimeData()
        self.mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        self.event = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            self.mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    def deliver_to(self, widget: Any) -> None:
        """把事件投递给控件的 ``dropEvent``。"""
        widget.dropEvent(self.event)


class DialogRecorder:
    """记录窗口弹出的对话框（无头测试中替代模态框）。

    ``summaries`` 只记录**真实实现会弹出**的结束提示（取消时不弹窗，与
    :meth:`MainWindow.show_summary_dialog` 一致），``summary_calls`` 记录全部调用。
    """

    def __init__(self) -> None:
        self.errors: list[tuple[str, str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.summaries: list[Any] = []
        self.summary_calls: list[Any] = []

    def _record_summary(self, summary: Any) -> None:
        self.summary_calls.append(summary)
        if not summary.cancelled:
            self.summaries.append(summary)

    def install(self, monkeypatch: pytest.MonkeyPatch, window: MainWindow) -> None:
        monkeypatch.setattr(
            window,
            "show_error_dialog",
            lambda src, dst, message: self.errors.append((src, dst, message)),
        )
        monkeypatch.setattr(
            window,
            "show_warning_dialog",
            lambda title, message: self.warnings.append((title, message)),
        )
        monkeypatch.setattr(window, "show_summary_dialog", self._record_summary)


@pytest.fixture
def dialogs(monkeypatch: pytest.MonkeyPatch, window: MainWindow) -> DialogRecorder:
    recorder = DialogRecorder()
    recorder.install(monkeypatch, window)
    return recorder


def wait_until_idle(window: MainWindow, wait_for: Any) -> None:
    assert wait_for(lambda: window.state == STATE_IDLE), "复制未在超时时间内结束"


# --------------------------------------------------------------------------- #
# 需求「单窗口中文界面布局」
# --------------------------------------------------------------------------- #


def test_window_has_three_section_layout(window: MainWindow) -> None:
    groups = window.findChildren(QGroupBox)
    assert [group.title() for group in groups] == [
        "① 源文件 / 文件夹",
        "② 目标目录与复制选项",
        "③ 进度与日志",
    ]
    assert window.source_list.acceptDrops() is True
    assert isinstance(window.add_files_button, QPushButton)
    assert isinstance(window.add_folder_button, QPushButton)
    assert isinstance(window.remove_button, QPushButton)
    assert isinstance(window.clear_button, QPushButton)
    assert isinstance(window.destination_edit, QLineEdit)
    assert isinstance(window.browse_button, QPushButton)
    assert isinstance(window.recursive_check, QCheckBox)
    assert isinstance(window.chunk_combo, QComboBox)
    assert isinstance(window.no_clobber_check, QCheckBox)
    assert isinstance(window.no_preserve_check, QCheckBox)
    assert window.progress_bar.value() == 0
    assert isinstance(window.current_file_label, QLabel)
    assert window.log_view.isReadOnly() is True
    assert window.start_button.text() == "开始"
    assert window.cancel_button.text() == "取消"
    assert window.windowTitle() == MainWindow.APP_TITLE


def test_all_visible_texts_are_chinese(window: MainWindow) -> None:
    texts: list[str] = [window.windowTitle()]
    texts += [group.title() for group in window.findChildren(QGroupBox)]
    texts += [button.text() for button in window.findChildren(QPushButton)]
    texts += [label.text() for label in window.findChildren(QLabel)]
    texts += [box.text() for box in window.findChildren(QCheckBox)]
    texts += [edit.placeholderText() for edit in window.findChildren(QLineEdit)]
    texts += [edit.placeholderText() for edit in window.findChildren(QPlainTextEdit)]
    texts += [window.status_label.text()]

    for text in texts:
        assert text.strip(), f"存在空文案: {text!r}"
        assert CJK.search(text), f"文案不是中文: {text!r}"
        assert not re.search(r"Button|Label|GroupBox|TODO|FIXME|None", text), text

    # 块大小下拉是「数字 + MiB 单位」，不含中文属正常，但必须都在 1~8 MiB 内。
    items = [window.chunk_combo.itemText(i) for i in range(window.chunk_combo.count())]
    assert items == ["1 MiB", "2 MiB", "4 MiB", "8 MiB"]


def test_initial_control_state(window: MainWindow) -> None:
    assert window.state == STATE_IDLE
    assert window.source_list.count() == 0
    assert window.start_button.isEnabled() is False, "源为空时「开始」必须禁用"
    assert window.cancel_button.isEnabled() is False, "未复制时「取消」必须禁用"
    assert window.status_label.text() == "状态：就绪"
    assert window.summary is None


def test_start_enabled_only_with_sources_and_destination(
    window: MainWindow, tmp_path: Path
) -> None:
    src = write_file(tmp_path / "a.txt", b"a")
    window.add_sources([src])
    assert window.start_button.isEnabled() is False, "目标未设置时「开始」必须禁用"

    window.destination_edit.setText(str(tmp_path))
    assert window.start_button.isEnabled() is True

    window.clear_sources()
    assert window.start_button.isEnabled() is False


# --------------------------------------------------------------------------- #
# 需求「源文件收集」
# --------------------------------------------------------------------------- #


def test_add_sources_dedupes_and_reports_count(window: MainWindow, tmp_path: Path) -> None:
    src = write_file(tmp_path / "a.txt", b"a")
    folder = tmp_path / "folder"
    folder.mkdir()

    assert window.add_sources([src, src, folder]) == 2
    assert window.add_sources([str(src)]) == 0, "重复路径必须去重"
    assert [path.name for path in window.source_paths()] == ["a.txt", "folder"]
    assert window.source_list.count() == 2
    assert window.source_list.item(0).toolTip() == str(src)


def test_choose_files_and_folder_use_dialogs(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_a = write_file(tmp_path / "a.txt", b"a")
    file_b = write_file(tmp_path / "b.txt", b"b")
    folder = tmp_path / "folder"
    folder.mkdir()

    monkeypatch.setattr(
        "transfertools.gui.main_window.QFileDialog.getOpenFileNames",
        lambda *args, **kwargs: ([str(file_a), str(file_b)], "文本文件"),
    )
    monkeypatch.setattr(
        "transfertools.gui.main_window.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: str(folder),
    )

    assert window.choose_files() == 2
    assert window.choose_folder() == 1
    assert [path.name for path in window.source_paths()] == ["a.txt", "b.txt", "folder"]


def test_choose_destination_sets_line_edit(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "transfertools.gui.main_window.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: str(tmp_path),
    )
    window.choose_destination()
    assert window.destination_edit.text() == str(tmp_path)


def test_remove_selected_and_clear(window: MainWindow, tmp_path: Path) -> None:
    first = write_file(tmp_path / "a.txt", b"a")
    second = write_file(tmp_path / "b.txt", b"b")
    window.add_sources([first, second])

    window.source_list.item(0).setSelected(True)
    assert window.remove_selected() == 1
    assert [path.name for path in window.source_paths()] == ["b.txt"]

    window.clear_sources()
    assert window.source_paths() == []
    assert "已清空源列表" in window.log_view.toPlainText()


def test_drop_event_adds_paths_and_dedupes(window: MainWindow, tmp_path: Path) -> None:
    """拖拽投放（spec「拖拽去重」）：注入真实 QDropEvent 到列表控件。"""
    src = write_file(tmp_path / "a.txt", b"a")
    folder = tmp_path / "folder"
    folder.mkdir()

    InjectedDrop([src, folder]).deliver_to(window.source_list)
    InjectedDrop([src]).deliver_to(window.source_list)
    InjectedDrop([folder, src]).deliver_to(window.source_list)

    assert [path.name for path in window.source_paths()] == ["a.txt", "folder"]
    assert window.source_list.count() == 2
    assert "已添加 2 个源" in window.log_view.toPlainText()


def test_drop_event_without_urls_is_ignored(window: MainWindow) -> None:
    InjectedDrop([]).deliver_to(window.source_list)
    assert window.source_list.count() == 0


def test_directory_source_requires_recursive_then_copies(
    window: MainWindow, dialogs: DialogRecorder, wait_for: Any, tmp_path: Path
) -> None:
    """spec「目录源需要递归开关」：未勾选时拒绝开始且不产生任何目标文件。"""
    source_dir = tmp_path / "tree"
    write_file(source_dir / "a.txt", b"a")
    destination = tmp_path / "out"
    destination.mkdir()

    window.add_sources([source_dir])
    window.destination_edit.setText(str(destination))
    window.start_copy()

    assert dialogs.warnings, "必须先给出提示"
    assert "递归" in dialogs.warnings[0][1]
    assert window.state == STATE_IDLE
    assert list(destination.iterdir()) == [], "拒绝开始时不得产生目标文件"

    window.recursive_check.setChecked(True)
    window.start_copy()
    wait_until_idle(window, wait_for)

    assert (destination / "a.txt").read_bytes() == b"a"
    assert window.summary is not None and window.summary.succeeded == 1


# --------------------------------------------------------------------------- #
# 需求「复制选项配置」
# --------------------------------------------------------------------------- #


def test_chunk_size_choices_and_default(window: MainWindow) -> None:
    chosen = [
        window.chunk_combo.itemData(index) for index in range(window.chunk_combo.count())
    ]
    assert chosen == [mib * MIB for mib in CHUNK_SIZE_CHOICES]
    assert all(MIB <= value <= 8 * MIB for value in chosen)
    assert window.chunk_combo.currentData() == 4 * MIB, "默认必须是 4 MiB"
    assert window.options().chunk_size == copier.DEFAULT_CHUNK_SIZE


def test_option_widgets_map_to_engine_options(window: MainWindow) -> None:
    default = window.options()
    assert default.recursive is False
    assert default.no_clobber is False
    assert default.preserve_metadata is True

    window.recursive_check.setChecked(True)
    window.chunk_combo.setCurrentIndex(
        [window.chunk_combo.itemData(i) for i in range(window.chunk_combo.count())].index(8 * MIB)
    )
    window.no_clobber_check.setChecked(True)
    window.no_preserve_check.setChecked(True)

    configured = window.options()
    assert configured.recursive is True
    assert configured.chunk_size == 8 * MIB
    assert configured.no_clobber is True
    assert configured.preserve_metadata is False


def test_options_locked_while_running_and_drops_rejected(
    window: MainWindow, dialogs: DialogRecorder, wait_for: Any, tmp_path: Path
) -> None:
    src = write_file(tmp_path / "big.bin", b"x" * (8 * MIB))
    destination = tmp_path / "out"
    destination.mkdir()
    window.add_sources([src])
    window.destination_edit.setText(str(destination))
    window.chunk_combo.setCurrentIndex(0)

    window.start_copy()
    assert window.state == STATE_RUNNING
    for widget in (
        window.add_files_button,
        window.add_folder_button,
        window.remove_button,
        window.clear_button,
        window.destination_edit,
        window.browse_button,
        window.recursive_check,
        window.chunk_combo,
        window.no_clobber_check,
        window.no_preserve_check,
        window.start_button,
    ):
        assert widget.isEnabled() is False, f"{widget} 在复制期间必须禁用"
    assert window.cancel_button.isEnabled() is True, "复制期间「取消」必须可用"
    assert window.log_view.isEnabled() is True, "日志区必须保持可滚动"
    assert window.source_list.acceptDrops() is False, "复制期间不接受拖放"
    assert window.add_sources([tmp_path / "other.txt"]) == 0

    window.request_cancel()
    wait_until_idle(window, wait_for)
    assert window.recursive_check.isEnabled() is True
    assert window.source_list.acceptDrops() is True


# --------------------------------------------------------------------------- #
# 需求「复制执行与进度展示」
# --------------------------------------------------------------------------- #


def test_full_copy_flow_updates_progress_log_and_summary(
    window: MainWindow, dialogs: DialogRecorder, wait_for: Any, tmp_path: Path
) -> None:
    data_a = b"a" * (MIB + 7)
    data_b = b"b" * 64
    src_a = write_file(tmp_path / "a.bin", data_a)
    src_b = write_file(tmp_path / "b.bin", data_b)
    destination = tmp_path / "out"
    destination.mkdir()

    window.add_sources([src_a, src_b])
    window.destination_edit.setText(str(destination))
    window.chunk_combo.setCurrentIndex(0)  # 1 MiB，制造多次字节进度回调

    window.start_copy()
    assert wait_for(lambda: window.state == STATE_IDLE)

    assert (destination / "a.bin").read_bytes() == data_a
    assert (destination / "b.bin").read_bytes() == data_b

    log = window.log_view.toPlainText()
    assert "写入中" in log and "校验中" in log
    assert "完成:" in log and "全部完成" in log
    assert "SHA-256" in log
    assert window.progress_bar.value() == 1000
    assert "已完成 2 个文件" in window.current_file_label.text()
    assert window.status_label.text().startswith("状态：全部完成")

    assert window.summary is not None
    assert window.summary.succeeded == 2 and window.summary.failed == 0
    assert window.summary.bytes_written == len(data_a) + len(data_b)
    assert len(dialogs.summaries) == 1, "全部成功应弹一次成功提示"
    assert dialogs.errors == []


# --------------------------------------------------------------------------- #
# 需求「错误呈现」
# --------------------------------------------------------------------------- #


def test_gui_stays_responsive_while_copying(
    window: MainWindow, dialogs: DialogRecorder, wait_for: Any, tmp_path: Path
) -> None:
    """spec「进度实时更新」：复制期间 GUI 线程仍在处理事件（界面不阻塞）。"""
    from PySide6.QtCore import QTimer

    src = write_file(tmp_path / "big.bin", b"x" * (64 * MIB))
    destination = tmp_path / "out"
    destination.mkdir()
    window.add_sources([src])
    window.destination_edit.setText(str(destination))
    window.chunk_combo.setCurrentIndex(0)

    fired: list[str] = []
    window.start_copy()
    # 定时器回调在复制进行中被执行 → 事件循环没有被复制阻塞。
    QTimer.singleShot(0, lambda: fired.append(window.state))
    assert wait_for(lambda: bool(fired), timeout=15), "复制期间事件循环未能处理事件"
    assert fired[0] == STATE_RUNNING

    assert window.log_view.isEnabled() is True
    window.request_cancel()
    wait_until_idle(window, wait_for)
    assert window.summary is not None and window.summary.cancelled is True


def test_file_error_shows_dialog_and_queue_continues(
    window: MainWindow, dialogs: DialogRecorder, wait_for: Any, tmp_path: Path
) -> None:
    """同源防护触发 → 错误对话框 + 日志，其余文件继续（与 CLI 语义一致）。"""
    destination = tmp_path / "out"
    destination.mkdir()
    same = write_file(destination / "same.txt", b"self")
    other = write_file(tmp_path / "other.txt", b"other")

    window.add_sources([same, other])
    window.destination_edit.setText(str(destination))

    window.start_copy()
    wait_until_idle(window, wait_for)

    assert len(dialogs.errors) == 1
    dialog_src, dialog_dst, dialog_message = dialogs.errors[0]
    assert dialog_src == str(same)
    assert dialog_dst == str(destination / "same.txt")
    assert "同一文件" in dialog_message

    log = window.log_view.toPlainText()
    assert "失败:" in log and "同一文件" in log

    assert (destination / "other.txt").read_bytes() == b"other"
    assert same.read_bytes() == b"self", "被拒绝时源文件必须零改动"
    assert window.summary is not None
    assert window.summary.failed == 1 and window.summary.succeeded == 1
    assert len(dialogs.summaries) == 1 and dialogs.summaries[0].failed == 1


def test_preflight_error_before_start_creates_nothing(
    window: MainWindow, dialogs: DialogRecorder, tmp_path: Path
) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    window.add_sources([tmp_path / "missing.txt"])
    window.destination_edit.setText(str(destination))

    window.start_copy()

    assert len(dialogs.warnings) == 1
    assert "源路径不存在" in dialogs.warnings[0][1]
    assert window.state == STATE_IDLE
    assert list(destination.iterdir()) == []
    assert window.start_button.isEnabled() is True


def test_real_error_dialog_uses_expected_text(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实对话框代码路径（monkeypatch QMessageBox.critical 以断言标题与正文）。"""
    captured: list[tuple[Any, str, str]] = []

    def fake_critical(parent: Any, title: str, text: str) -> None:
        captured.append((parent, title, text))

    monkeypatch.setattr(QMessageBox, "critical", staticmethod(fake_critical))
    window.show_error_dialog("/src/a.txt", "/dst/a.txt", "完整性校验失败")

    assert len(captured) == 1
    parent, title, text = captured[0]
    assert parent is window
    assert title == "复制失败"
    assert "/src/a.txt" in text and "/dst/a.txt" in text
    assert "完整性校验失败" in text
    assert "其余文件将继续" in text


def test_real_summary_dialogs_variants(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        staticmethod(lambda parent, title, text: calls.append(("info", title, text))),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda parent, title, text: calls.append(("warn", title, text))),
    )

    from transfertools.gui.controller import CopyFailure, CopySummary

    success = CopySummary(2, 2, 0, False, 3 * MIB)
    window.show_summary_dialog(success)
    assert calls[-1][0] == "info"
    assert "成功复制 2 个文件" in calls[-1][2]

    empty = CopySummary(0, 0, 0, False, 0)
    window.show_summary_dialog(empty)
    assert calls[-1][1] == "没有需要复制的文件"

    failed = CopySummary(
        total=3,
        succeeded=1,
        failed=2,
        cancelled=False,
        bytes_written=10,
        failures=[
            CopyFailure(Path("/s/a"), Path("/d/a"), "原因一"),
            CopyFailure(Path("/s/b"), Path("/d/b"), "原因二"),
        ],
    )
    window.show_summary_dialog(failed)
    assert calls[-1][0] == "warn"
    assert calls[-1][1] == "复制完成但存在失败"
    assert "原因一" in calls[-1][2] and "原因二" in calls[-1][2]

    before = len(calls)
    window.show_summary_dialog(CopySummary(3, 1, 0, True, 5))
    assert len(calls) == before, "取消不弹窗（用户主动操作，日志已足够）"


def test_dialog_text_helpers() -> None:
    assert "文件: /s/a" in format_error_dialog("/s/a", "/d/a", "原因")
    assert "其余文件将继续" in format_error_dialog("/s/a", "/d/a", "原因")

    from transfertools.gui.controller import CopyFailure, CopySummary

    summary = CopySummary(
        total=12,
        succeeded=0,
        failed=12,
        cancelled=False,
        bytes_written=0,
        failures=[
            CopyFailure(Path(f"/s/{index}"), Path(f"/d/{index}"), "原因")
            for index in range(12)
        ],
    )
    text = format_summary_dialog(summary)
    assert "失败条目" in text
    assert text.count("原因") == 10, "对话框最多列出 10 条"
    assert "其余 2 条详见日志区" in text


# --------------------------------------------------------------------------- #
# 需求「取消与半成品清理」
# --------------------------------------------------------------------------- #


def test_cancel_during_copy_removes_partial_and_restores_idle(
    window: MainWindow, dialogs: DialogRecorder, wait_for: Any, tmp_path: Path
) -> None:
    src_big = write_file(tmp_path / "big.bin", b"x" * (16 * MIB))
    src_small = write_file(tmp_path / "small.bin", b"y" * 32)
    destination = tmp_path / "out"
    destination.mkdir()

    window.add_sources([src_big, src_small])
    window.destination_edit.setText(str(destination))
    window.chunk_combo.setCurrentIndex(0)

    window.start_copy()
    assert window.state == STATE_RUNNING
    window.request_cancel()
    assert window.cancel_button.text() == "正在取消…"
    assert window.cancel_button.isEnabled() is False

    wait_until_idle(window, wait_for)

    assert window.summary is not None
    assert window.summary.cancelled is True
    assert window.summary.succeeded == 0
    assert not (destination / "big.bin").exists(), "半成品必须被删除"
    assert not (destination / "small.bin").exists(), "剩余文件不得开始"

    log = window.log_view.toPlainText()
    assert "已请求取消" in log and "已取消" in log
    assert dialogs.errors == [], "取消不是错误，不应弹错误对话框"
    assert dialogs.summaries == [], "取消不弹汇总对话框"

    assert window.state == STATE_IDLE
    assert window.cancel_button.text() == "取消" and window.cancel_button.isEnabled() is False
    assert window.start_button.isEnabled() is True, "取消后必须能再次开始"
    assert window.recursive_check.isEnabled() is True


def test_can_start_again_after_cancel(
    window: MainWindow, dialogs: DialogRecorder, wait_for: Any, tmp_path: Path
) -> None:
    src_big = write_file(tmp_path / "big.bin", b"x" * (16 * MIB))
    src_small = write_file(tmp_path / "small.bin", b"y" * 32)
    destination = tmp_path / "out"
    destination.mkdir()

    window.add_sources([src_big])
    window.destination_edit.setText(str(destination))
    window.start_copy()
    window.request_cancel()
    wait_until_idle(window, wait_for)
    assert window.summary is not None and window.summary.cancelled is True

    # 去掉大文件后重新开始，应完整成功。
    window.clear_sources()
    window.add_sources([src_small])
    window.start_copy()
    wait_until_idle(window, wait_for)

    assert window.summary is not None
    assert window.summary.succeeded == 1 and window.summary.cancelled is False
    assert (destination / "small.bin").read_bytes() == b"y" * 32
    assert window.progress_bar.value() == 1000
    assert len(dialogs.summaries) == 1
    assert dialogs.summaries[0].succeeded == 1
    assert format_bytes(len(b"y" * 32)) == "32 字节"
