# Proposal

## Why

safecopy 目前只有命令行接口，目标用户（Windows 10+ 桌面用户，常需向外部存储拷贝文件）不熟悉命令行，且拖拽、进度可视化、错误提示等桌面交互缺失，导致工具难以推广。需要一个图形界面，在不改变既有复制引擎语义的前提下提供桌面化操作体验，并能打包为单文件 Windows 可执行程序分发。

## What Changes

- 新增基于 PySide6 的 GUI 前端 `safecopy-gui`：单窗口三段式布局（源列表区 / 目标与选项区 / 进度日志区），中文界面，支持多选文件/文件夹与拖拽投放。
- GUI 复用 `transfertools.copier` 复制引擎；为支持进度显示与取消，允许在引擎上新增进度回调与取消事件钩子，但**不得改变既有复制语义、完整性校验流程与 CLI 退出码**（非 BREAKING）。
- 新增 GUI 侧选项映射：递归开关、块大小 1~8 MiB、no-clobber、no-preserve-metadata，与 CLI 选项一一对应。
- 异常呈现：`SafeCopyError`（同源、递归自复制、校验失败等）以模态对话框 + 日志区双重展示；取消操作立即生效并依赖引擎既有逻辑清理半成品文件。
- 新增打包交付：PyInstaller onefile + windowed 产出 `safecopy-gui.exe`（含版本与图标配置），提供 `.spec` 文件、Windows 本机打包脚本、`.github/workflows` 中 windows-latest CI 自动构建并上传 artifact。
- 测试策略：GUI 业务逻辑与控件分离，支持 `QT_QPA_PLATFORM=offscreen` 无头 UI 测试；Linux 宿上以 offscreen 测试 + Linux onedir 冒烟构建代替本机 Windows exe 验收。

## Capabilities

### New Capabilities

- `safecopy-gui`: 图形界面能力——源文件/目录收集与拖拽、复制选项配置、进度与日志展示、开始/取消控制、错误对话框呈现，以及 GUI 的 Windows 打包交付。

### Modified Capabilities

（无。引擎新增进度回调/取消钩子属于实现细节扩展，不改变 `safe-file-copy` 既有需求的外部行为。）

## Impact

- **代码**：新增 `src/transfertools/gui/`（窗口、控制器、工作线程等）；`src/transfertools/copier.py` 可能增加取消事件参数（向后兼容的可选参数）；`pyproject.toml` 增加可选依赖 `gui = ["PySide6"]` 与打包相关 dev 依赖（PyInstaller）。
- **依赖**：运行时不新增 CLI 依赖——PySide6 仅作为 GUI extras；CLI (`safecopy`) 在未安装 PySide6 时行为不变。
- **CI/交付**：新增 `.github/workflows/build-exe.yml`（windows-latest 构建 exe artifact）、PyInstaller `.spec`、Windows 本机打包脚本。
- **测试**：新增 offscreen UI 测试与引擎取消钩子测试；既有 `tests/test_no_copy_api.py` 静态断言必须继续通过。
