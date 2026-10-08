# Design

## Context

现有代码：`src/transfertools/copier.py` 已实现「创建目标 → 分块写入 → fsync → SHA-256 双端比对 → 元数据保留」的复制引擎，暴露 `copy_file(src, dst, *, chunk_size, preallocate, preserve_metadata, warn, progress)` 与 `SafeCopyError` 异常族（`SourceError` / `DestinationError` / `SameFileError` / `VerificationError` / `InvalidChunkSizeError`）；`cli.py` 在其上做编排（预检、多文件循环、递归遍历、退出码 0/1/2）。`tests/test_no_copy_api.py` 对包源码做静态断言，禁止直接复制 API 回归。GUI 要在不破坏这些既有行为的前提下增加桌面交互层。动机见 proposal.md。

## Goals / Non-Goals

**Goals:**

- 单窗口中文 GUI，复用 `transfertools.copier` 引擎完成全部实际复制工作。
- 进度可见（总进度 + 当前文件 + 日志）、可取消（半成品自动清理）、错误以对话框 + 日志呈现。
- 业务逻辑与控件分离，全部非控件逻辑可在 `QT_QPA_PLATFORM=offscreen` 下无头测试。
- 打包链路：PyInstaller onefile + windowed 产出 `safecopy-gui.exe`，CI（windows-latest）自动构建 artifact，附 Windows 本机打包脚本。

**Non-Goals:**

- 不改变 `safe-file-copy` 既有复制语义、校验流程与 CLI 退出码；CLI 不新增任何运行时依赖。
- 不做多窗口/向导、不做复制队列的持久化、不做跨平台主题适配、不做暂停/续传。
- 不在 Linux 宿主上产出 Windows exe（见 D-g5）。
- 不处理引擎层未支持的语义（如 ACL/xattr 保留、符号链接跟随策略变更）。

## Decisions

### D-g1：技术栈 = PySide6

GUI 使用 PySide6（Qt for Python，LGPL）。理由：

- **原生拖拽**：`QListWidget`/自定义视图设置 `setAcceptDrops` 即可接收 OS 级文件拖放（`mimeData().urls()`），满足源列表拖拽投放需求。
- **HiDPI**：Qt6 在 Windows 上默认启用高 DPI 缩放，外部存储场景常见的高分屏无需额外处理。
- **QThread + 信号槽**：复制工作放入 `QThread` worker，进度/日志经 Qt 信号跨线程投递到 GUI 线程，天然满足「复制不阻塞界面」。
- **无头测试**：`QT_QPA_PLATFORM=offscreen` 下 pytest + pytest-qt（或直接驱动 `QApplication`）可实例化并驱动窗口做 UI 测试，Linux CI/宿主的验收可行。
- 相比 tkinter（外观老旧、拖拽需第三方库）与 PyQt6（GPL/商业双许可），PySide6 的 LGPL 对分发闭源 exe 更友好。

依赖管理：PySide6 放入 `pyproject.toml` 的可选 extras（如 `gui = ["PySide6>=6.6"]`），PyInstaller 放入 dev 依赖组。**CLI 运行时不新增依赖**——未安装 extras 时 `safecopy` CLI 行为完全不变；GUI 入口对 `ImportError` 做友好提示（spec「与 CLI 共存」）。

### D-g2：布局 = 单窗口三段式（中文界面）

主窗口 `QMainWindow`（或 `QWidget` + `QVBoxLayout`）自上而下：

1. **源列表区**：`QListWidget`（接受拖放）+ 按钮行「添加文件…」「添加文件夹…」「移除选中」「清空」。
2. **目标与选项区**：目标目录 `QLineEdit` +「浏览…」按钮；选项行：「递归复制目录」复选框、块大小 `QComboBox`（1/2/4/8 MiB，默认 4 MiB）、「目标已存在时跳过（no-clobber）」复选框、「不保留权限与时间戳（no-preserve-metadata）」复选框。
3. **进度与日志区**：总进度条 `QProgressBar`、当前文件 `QLabel`、只读日志 `QPlainTextEdit`、底部「开始」「取消」按钮。

状态机：`idle → running → (done | cancelled | failed)`。`running` 期间锁定全部输入控件，仅「取消」可用；结束后恢复 `idle` 可编辑。界面文案全部中文。

### D-g3：架构 = GUI 壳复用引擎 + 控制器分离

分层：

- **引擎钩子（`transfertools.copier`，向后兼容扩展）**：
  - 细粒度进度：现有 `progress(stage)` 只有 write/verify/done 三个阶段。GUI 需要字节级进度，因此在 `copy_file` 增加**可选** `on_bytes(written, total)` 回调（每写完一块调用一次）；不传时行为与现状逐字节一致。
  - 取消：增加**可选** `cancel_event: threading.Event | None` 参数，在分块写入循环与校验循环的每次迭代检查，置位时抛出 `SafeCopyError` 子类（如 `CopyCancelledError`）。取消走既有 `except BaseException` 清理路径删除半成品，因此「取消即清理」复用已验证的 D4 语义。
  - 两个钩子均为可选关键字参数，默认值下引擎行为与现有 spec（safe-file-copy）逐点一致；CLI 不传入，退出码不变。
- **编排层（GUI 侧，`transfertools/gui/controller.py`，不依赖 Qt）**：把「预检（复用 `cli._preflight` 等价逻辑）、展开源列表为 (src, dst) 任务、逐任务调用 `copy_file`、汇总成功/失败」做成纯 Python 控制器，输入输出为数据类与回调。预检逻辑与 CLI 共享同一实现（抽到公共模块或复用 `cli` 内部函数），保证 GUI 与 CLI 语义一致。
- **工作线程（`transfertools/gui/worker.py`）**：`QObject` worker 持有控制器，在 `QThread` 中运行；把控制器的回调（字节进度、文件开始/完成、日志行、错误、取消确认）转成 Qt 信号。
- **视图层（`transfertools/gui/main_window.py`）**：只做控件装配、信号连接、控件状态切换；不含复制逻辑。拖拽的 `dragEnterEvent/dropEvent` 仅负责提取路径列表并交给控制器去重/校验。

该分层使控制器与 worker 的全部行为可在 offscreen/无 QApplication 或最小 QApplication 环境下测试（spec「Linux 宿主替代验收」）。

错误呈现：worker 捕获 `SafeCopyError`/`OSError` → 发 `errorOccurred(src, dst, message)` 信号 → 视图弹 `QMessageBox.critical` 并追加日志；单文件失败不中断循环（与 CLI 的逐文件容错一致）。取消：`threading.Event` 由「取消」按钮置位；引擎在块边界响应（最坏延迟 = 一个块 + fsync，可接受）；`CopyCancelledError` 在 GUI 汇总为「已取消」而非失败。

### D-g4：打包 = PyInstaller onefile + windowed → safecopy-gui.exe

- 提供 `packaging/safecopy-gui.spec`：`onefile`、`windowed`（`console=False`）、`name="safecopy-gui"`，内嵌版本信息（PyInstaller 的 `version=` 指向生成的 version resource 文件）与 `icon=` 图标（提交一个 `packaging/assets/safecopy-gui.ico`；若无正式图标，以脚本生成简单占位 ICO 并在 README 注明可替换）。`hiddenimports` 按需补 PySide6 插件。
- 入口脚本 `src/transfertools/gui/__main__.py`，使 `python -m transfertools.gui` 与打包入口一致。
- Windows 本机打包脚本 `packaging/build-exe.ps1`（PowerShell）：创建/复用 venv → `pip install .[gui] pyinstaller` → `pyinstaller packaging/safecopy-gui.spec` → 校验 `dist/safecopy-gui.exe` 存在。
- CI：`.github/workflows/build-exe.yml`，`runs-on: windows-latest`，触发于 push tag（v*）/ 手动 `workflow_dispatch`；步骤：checkout → setup-python 3.13 → 安装依赖（`.[gui]` + pyinstaller + pytest）→ offscreen pytest → pyinstaller 构建 → `actions/upload-artifact` 上传 `dist/safecopy-gui.exe`（artifact 名 `safecopy-gui-windows`）。

### D-g5：验收现实性 = Linux 宿主只做替代验收

Linux 宿主无法产出 Windows exe（PyInstaller 不做交叉编译）。因此本机验收为：

1. `QT_QPA_PLATFORM=offscreen pytest`：控制器单元测试 + 主窗口 UI 测试（添加/拖拽路径、状态机切换、错误对话框、取消流程——用临时目录真实复制小文件）。
2. Linux onedir 冒烟构建：`pyinstaller --onedir` 在本机跑通，验证 `.spec` 的资源收集与 import 完整性（onedir 与 onefile 共享分析阶段，足以暴露漏打包）。
3. Windows exe 的实际产出与人工验收由 GitHub Actions（windows-latest artifact）或用户 Windows 本机脚本完成，并在 README/交付说明中写明此边界。

## Risks / Trade-offs

- [PySide6 使仓库依赖体积与打包产物显著增大（exe 约 40~60MB）] → 作为可选 extras 隔离，CLI 用户零成本；CI 缓存 pip 依赖。
- [LGPL 动态链接义务：onefile 打包 Qt 需允许用户替换 Qt 库] → 在 README 增加许可与合规说明（PySide6 LGPL 声明、Qt 库替换途径）。
- [取消最坏延迟为一个块写入 + 校验读回（fsync 可能较慢）] → 块大小上限 8 MiB 使延迟有界；UI 上「取消」置位后立即显示「正在取消…」反馈。
- [引擎新增回调参数存在回归风险] → 默认值下行为不变；`tests/test_no_copy_api.py` 与既有 copier/cli 测试必须全绿，并新增钩子单测（取消置位 → 半成品已删除）。
- [递归模式下 GUI 需自行展开目录为文件列表以便显示总进度] → 控制器先做只读遍历统计文件数与总字节（统计失败降级为仅按文件数显示进度），实际复制仍逐文件走引擎。
- [拖拽来源复杂（UNC 路径、带引号路径）] → 统一经 `pathlib.Path` 规范化，控制器复用与 CLI 相同的路径校验。
- [Linux offscreen 测试与真实 Windows 行为存在差异（文件系统、路径分隔符）] → 路径处理全部经 `pathlib`；CI 的 Windows 构建前在 windows-latest 上运行完整 pytest 套件兜底。

## Migration Plan

纯新增能力，无数据或接口迁移：

1. 引擎增加可选钩子（向后兼容）→ 既有测试全绿即合入。
2. 新增 `transfertools/gui/` 包、extras 与测试。
3. 新增 `.spec`、打包脚本、CI 工作流；首个 tag 触发 CI 产出 `safecopy-gui.exe`。
4. 回滚：GUI 相关文件均为新增，删除即回滚；引擎钩子参数为可选，保留不影响 CLI。

## Open Questions

- 正式图标素材尚未提供；首个版本可用脚本生成的占位 ICO，后续替换不影响打包链路（仅换 `packaging/assets/` 文件）。
- exe 是否需要代码签名：暂不在范围内，若目标环境 SmartScreen 拦截严重再单独立项。
