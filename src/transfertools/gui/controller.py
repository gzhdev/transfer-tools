"""GUI 编排层（design.md D-g3）：不依赖 Qt 的纯 Python 控制器。

职责：

- **预检**：复用 :mod:`transfertools.preflight` 的规则（与 CLI 同一套实现），
  源失效、目录源未启用递归、目标目录非法、递归自复制等问题在动手前一次性报出；
- **展开**：把源列表展开成 ``(src, dst)`` 任务列表（目录源按 ``os.walk`` 重建目录树，
  与 ``cli._run_recursive`` 的顺序/结构一致），并统计文件数与总字节数；
- **执行**：逐个任务调用 :func:`transfertools.copier.copy_file`（引擎钩子
  ``on_bytes`` / ``cancel_event``），单文件失败不中断后续任务；
- **上报**：进度、日志、错误经 :class:`ProgressListener` 回调交给 Qt 侧桥接。

取消协议（tasks 3.2）：:meth:`CopyController.request_cancel` 置位
:class:`threading.Event`，正在复制的文件在块边界停止、半成品由引擎既有清理路径删除，
剩余任务不再开始，汇总状态为「已取消」。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .. import copier, preflight
from ..copier import SafeCopyError
from ..preflight import UsageError

__all__ = [
    "DEFAULT_RECURSIVE",
    "CopyFailure",
    "CopyOptions",
    "CopyPlan",
    "CopySummary",
    "CopyTask",
    "CopyController",
    "ProgressListener",
    "CHUNK_SIZE_CHOICES",
    "dedupe_paths",
    "format_bytes",
    "format_chunk_size",
    "preflight_gui",
]

#: 递归复选框的默认状态（与 CLI 需要显式 ``-r`` 的语义不同：GUI 里由用户勾选）。
DEFAULT_RECURSIVE = False

#: 块大小下拉的档位（MiB → 字节），默认在视图侧取 4 MiB（spec「复制选项配置」）。
CHUNK_SIZE_CHOICES: tuple[int, ...] = (1, 2, 4, 8)


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CopyOptions:
    """GUI 选项 → 引擎参数的映射（与 CLI 选项一一对应）。"""

    recursive: bool = DEFAULT_RECURSIVE
    chunk_size: int = copier.DEFAULT_CHUNK_SIZE
    #: 「目标已存在时跳过（no-clobber）」；与 CLI 一致：跳过计为失败。
    no_clobber: bool = False
    #: 「保留权限与时间戳」的反选项，与 CLI ``--no-preserve-metadata`` 对应。
    preserve_metadata: bool = True


@dataclass(frozen=True)
class CopyTask:
    """一个待复制条目（单文件；目录源已在展开阶段拆开）。"""

    src: Path
    dst: Path


@dataclass(frozen=True)
class CopyPlan:
    """预检通过后的执行计划。"""

    tasks: tuple[CopyTask, ...]
    total_files: int
    #: 全部任务的总字节数；:attr:`bytes_known` 为 False 时该值不完整。
    total_bytes: int
    #: 是否成功统计了所有源文件大小（统计失败时进度降级为按文件数计）。
    bytes_known: bool


@dataclass(frozen=True)
class CopyFailure:
    """单个文件的失败条目（含被 no-clobber 跳过的条目，与 CLI 计数一致）。"""

    src: Path
    dst: Path
    message: str
    skipped: bool = False


@dataclass
class CopySummary:
    """一次复制（或取消）的最终汇总。"""

    total: int
    succeeded: int
    failed: int
    cancelled: bool
    bytes_written: int
    failures: list[CopyFailure] = field(default_factory=list)
    skipped: int = 0
    elapsed: float = 0.0

    @property
    def unfinished(self) -> int:
        """未完成的文件数（含取消时正在复制、其半成品已被删除的那个）。"""
        return max(self.total - self.succeeded - self.failed, 0)

    def describe(self) -> str:
        """中文汇总文案（日志区与状态标签共用）。"""
        if self.cancelled:
            return (
                f"已取消：成功 {self.succeeded} 个，失败 {self.failed} 个，"
                f"未完成 {self.unfinished} 个（共 {self.total} 个文件）。"
            )
        if self.total == 0:
            return "没有需要复制的文件。"
        if self.failed == 0:
            return (
                f"全部完成：成功 {self.succeeded} 个文件，"
                f"共 {format_bytes(self.bytes_written)}，耗时 {self.elapsed:.1f} 秒。"
            )
        detail = f"（其中目标已存在跳过 {self.skipped} 个）" if self.skipped else ""
        return (
            f"完成但存在失败：成功 {self.succeeded} 个，失败 {self.failed} 个{detail}，"
            f"共写入 {format_bytes(self.bytes_written)}，耗时 {self.elapsed:.1f} 秒。"
        )


class ProgressListener:
    """控制器回调集合。

    Qt 侧由 :class:`transfertools.gui.worker.CopyWorker` 覆盖为信号发射；测试可直接
    子类化记录调用序列，因此本模块无需 Qt 即可完整测试。
    """

    def on_plan(self, plan: CopyPlan) -> None:  # pragma: no cover - 默认空实现
        """预检通过、开始执行前调用（视图据此设置进度条量程）。"""

    def on_file_start(  # pragma: no cover - 默认空实现
        self, index: int, total_files: int, src: Path, dst: Path
    ) -> None:
        """某个文件开始复制（index 从 1 开始）。"""

    def on_file_progress(self, written: int, total: int) -> None:  # pragma: no cover
        """当前文件已写入字节数（每写完一块回调一次）。"""

    def on_progress(self, done: int, total: int) -> None:  # pragma: no cover
        """总体进度；单位由 ``plan.bytes_known`` 决定（字节，或文件数）。"""

    def on_log(self, message: str) -> None:  # pragma: no cover - 默认空实现
        """一行中文日志。"""

    def on_file_error(  # pragma: no cover - 默认空实现
        self, src: Path, dst: Path, message: str
    ) -> None:
        """单个文件失败（视图弹模态对话框 + 写日志；不中断其它文件）。"""

    def on_summary(self, summary: CopySummary) -> None:  # pragma: no cover
        """执行结束（无论成功、失败或取消）后的汇总。"""


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def format_bytes(size: int) -> str:
    """人类可读的字节数（中文单位，保留 1 位小数）。"""
    if size < 1024:
        return f"{size} 字节"
    for unit, factor in (("KiB", 1024), ("MiB", 1024**2), ("GiB", 1024**3)):
        if size < factor * 1024:
            return f"{size / factor:.1f} {unit}"
    return f"{size / 1024**4:.1f} TiB"


def format_chunk_size(chunk_size: int) -> str:
    """块大小的中文档位文案，例如 ``4 MiB``。"""
    return f"{chunk_size // copier.MIB} MiB"


def dedupe_paths(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    """按规范化绝对路径去重（spec「源文件收集」：重复添加同一路径只保留一份）。

    保留用户可见的原始写法（便于列表展示），仅用规范化路径判定重复；符号链接与
    ``dir/../f`` 这类写法不同的同一文件也会被识别为重复。
    """
    result: list[Path] = []
    seen: set[str] = set()
    for item in paths:
        path = Path(item)
        key = copier.canonical_path(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def preflight_gui(
    sources: Sequence[Path], destination: Path | None, *, recursive: bool
) -> None:
    """开始复制前的一次性校验（与 CLI 语义一致，消息取自 :mod:`transfertools.preflight`）。

    GUI 与 CLI 的差异只有两点，均因界面语义而来：目标永远是「目录」（CLI 允许显式的
    单文件目标路径），且一次可混合提交文件与目录（CLI 的 ``-r`` 只接受一个源目录）。

    :raises UsageError: 源为空、源失效、目录源未启用递归、目标目录非法或递归自复制。
    """
    if not sources:
        raise UsageError("源列表为空，请先添加要复制的文件或文件夹。")
    if destination is None:
        raise UsageError("尚未设置目标目录，请先选择或输入目标目录。")

    preflight.check_sources_exist(sources)
    if not recursive:
        # 递归模式下目录源是允许的（GUI 会把它展开成逐个文件的复制任务）。
        preflight.check_directories_need_recursive(sources)

    if destination.exists() and not destination.is_dir():
        raise UsageError(f"目标路径已存在且不是目录: {destination}")

    # 自复制防护优先于「目标目录必须在」这类便利性检查：等价于 CLI 递归场景的检查
    # （只是对多个目录源逐一应用），目标尚不存在时同样能拦截。
    for item in sources:
        if item.is_dir():
            preflight.check_self_copy(item, destination)

    if not destination.is_dir():
        raise UsageError(f"目标目录不存在（请先创建或重新选择）: {destination}")


# --------------------------------------------------------------------------- #
# 控制器
# --------------------------------------------------------------------------- #


class CopyController:
    """把源列表 + 选项变成一次（可取消的）复制执行。

    一个实例对应一次执行：视图每次点击「开始」都新建控制器与工作线程。
    """

    def __init__(
        self,
        options: CopyOptions | None = None,
        listener: ProgressListener | None = None,
    ) -> None:
        self.options = options or CopyOptions()
        self.listener = listener or ProgressListener()
        self._cancel_event = threading.Event()
        #: 当前正在复制的任务（供引擎回调补全日志文案）。
        self._current_task: CopyTask | None = None
        #: :meth:`run_plan` 注入的「总体进度」上报闭包。
        self._total_progress_sink: Callable[[int], None] | None = None

    # -- 取消 -------------------------------------------------------------- #

    def request_cancel(self) -> None:
        """请求取消（线程安全）：置位事件，引擎在块边界响应。"""
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    # -- 计划 -------------------------------------------------------------- #

    def build_plan(
        self, sources: Sequence[str | os.PathLike[str]], destination: str | os.PathLike[str]
    ) -> CopyPlan:
        """预检并展开源列表为任务计划。

        :raises UsageError: 预检失败（不产生任何目标文件）。
        """
        src_paths = [Path(item) for item in sources]
        dst_path = Path(destination)
        preflight_gui(src_paths, dst_path, recursive=self.options.recursive)

        tasks: list[CopyTask] = []
        for src in src_paths:
            if src.is_dir():
                tasks.extend(self._expand_directory(src, dst_path))
            else:
                tasks.append(CopyTask(src=src, dst=copier.resolve_destination(src, dst_path)))

        total_bytes = 0
        bytes_known = True
        for task in tasks:
            try:
                total_bytes += task.src.stat().st_size
            except OSError:
                bytes_known = False
        return CopyPlan(
            tasks=tuple(tasks),
            total_files=len(tasks),
            total_bytes=total_bytes if bytes_known else 0,
            bytes_known=bytes_known,
        )

    @staticmethod
    def _expand_directory(src_dir: Path, dst_dir: Path) -> list[CopyTask]:
        """递归展开目录源（顺序与 ``cli._run_recursive`` 一致：目录名与文件名均排序）。"""
        tasks: list[CopyTask] = []
        for current, dirnames, filenames in os.walk(src_dir):
            dirnames.sort()
            current_path = Path(current)
            target_dir = dst_dir / current_path.relative_to(src_dir)
            for name in sorted(filenames):
                tasks.append(CopyTask(src=current_path / name, dst=target_dir / name))
        return tasks

    # -- 执行 -------------------------------------------------------------- #

    def run(
        self, sources: Sequence[str | os.PathLike[str]], destination: str | os.PathLike[str]
    ) -> CopySummary:
        """预检 + 执行（阻塞调用；Qt 侧放在 ``QThread`` 中运行）。

        :raises UsageError: 预检失败（不产生任何目标文件）。
        """
        return self.run_plan(self.build_plan(sources, destination))

    def run_plan(self, plan: CopyPlan) -> CopySummary:
        """执行已经过预检的任务计划，返回汇总（不抛异常：逐文件容错）。"""
        listener = self.listener
        listener.on_plan(plan)

        started = time.monotonic()
        failures: list[CopyFailure] = []
        succeeded = 0
        skipped = 0
        files_done = 0
        done_bytes = 0
        cancelled = False
        # 总字节数已知且非零时按字节显示总进度；否则降级为按文件数（tasks 3.1）。
        use_bytes = plan.bytes_known and plan.total_bytes > 0

        def report_total(written_in_file: int) -> None:
            if use_bytes:
                listener.on_progress(done_bytes + written_in_file, plan.total_bytes)
            else:
                listener.on_progress(files_done, plan.total_files)

        def fail(task: CopyTask, message: str, *, skipped_item: bool = False) -> None:
            failures.append(
                CopyFailure(task.src, task.dst, message, skipped=skipped_item)
            )
            listener.on_log(f"失败: {task.src} -> {task.dst}: {message}")
            listener.on_file_error(task.src, task.dst, message)

        self._total_progress_sink = report_total
        try:
            report_total(0)
            for index, task in enumerate(plan.tasks, start=1):
                if self._cancel_event.is_set():
                    cancelled = True
                    listener.on_log(
                        f"已取消：剩余 {plan.total_files - index + 1} 个文件不再处理。"
                    )
                    break

                self._current_task = task
                listener.on_file_start(index, plan.total_files, task.src, task.dst)
                listener.on_log(f"[{index}/{plan.total_files}] 开始: {task.src} -> {task.dst}")

                try:
                    task.dst.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    fail(task, f"创建目标目录失败 {task.dst.parent}: {exc}")
                    files_done += 1
                    report_total(0)
                    continue

                if self.options.no_clobber and task.dst.exists():
                    skipped += 1
                    fail(
                        task,
                        f"目标已存在，已跳过（no-clobber）: {task.dst}",
                        skipped_item=True,
                    )
                    files_done += 1
                    report_total(0)
                    continue

                try:
                    result = copier.copy_file(
                        task.src,
                        task.dst,
                        chunk_size=self.options.chunk_size,
                        preserve_metadata=self.options.preserve_metadata,
                        warn=self._warn,
                        progress=self._stage,
                        on_bytes=self._on_bytes,
                        cancel_event=self._cancel_event,
                    )
                except copier.CopyCancelledError:
                    # 取消不是错误：半成品已由引擎清理，汇总为「已取消」（design D-g3）。
                    cancelled = True
                    listener.on_log(f"已取消: {task.src}（目标上的半成品已删除）")
                    break
                except (SafeCopyError, OSError) as exc:
                    fail(task, str(exc))
                    files_done += 1
                    report_total(0)
                    continue

                succeeded += 1
                files_done += 1
                done_bytes += result.size
                listener.on_log(
                    f"完成: {result.dst}（{format_bytes(result.size)}，"
                    f"SHA-256 {result.sha256[:16]}…）"
                )
                report_total(0)
        finally:
            self._current_task = None
            self._total_progress_sink = None

        summary = CopySummary(
            total=plan.total_files,
            succeeded=succeeded,
            failed=len(failures),
            cancelled=cancelled,
            bytes_written=done_bytes,
            failures=failures,
            skipped=skipped,
            elapsed=time.monotonic() - started,
        )
        listener.on_summary(summary)
        listener.on_log(summary.describe())
        return summary

    # -- 引擎回调桥接 ------------------------------------------------------ #

    def _on_bytes(self, written: int, total: int) -> None:
        self.listener.on_file_progress(written, total)
        sink = self._total_progress_sink
        if sink is not None:
            sink(written)
        else:  # pragma: no cover - 仅在绕过 run_plan 直接驱动引擎时出现
            self.listener.on_progress(written, total)

    def _stage(self, stage: str) -> None:
        """引擎阶段 → 日志行（与 ``cli._make_progress`` 的文案一致）。"""
        labels = {copier.STAGE_WRITE: "写入中", copier.STAGE_VERIFY: "校验中"}
        label = labels.get(stage)
        if label is None:
            return
        task = self._current_task
        if task is None:  # pragma: no cover - 理论上不会发生
            self.listener.on_log(f"{label}…")
        else:
            self.listener.on_log(f"{label}: {task.src} -> {task.dst}")

    def _warn(self, message: str) -> None:
        self.listener.on_log(f"警告: {message}")
