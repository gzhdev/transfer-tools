# Tasks

## 1. 引擎钩子扩展（向后兼容）

- [x] 1.1 在 `copier.py` 增加可选关键字参数 `on_bytes(written, total)`（分块写入循环每写完一块回调一次）与 `cancel_event: threading.Event | None`（写入与校验循环每次迭代检查，置位时抛新增 `CopyCancelledError(SafeCopyError)`），取消走既有 `except BaseException` 半成品清理路径；验证：`pytest tests/test_copier.py` 全绿（既有调用点零改动），新增钩子单测通过——含「取消置位后半成品文件已删除」「不传钩子参数时行为与现状一致」用例
- [x] 1.2 确认 CLI 不传入新参数且行为不变；验证：`pytest tests/test_cli.py tests/test_no_copy_api.py` 全绿，CLI 各场景退出码（0/1/2）与变更前一致

## 2. 依赖与入口配置

- [x] 2.1 在 `pyproject.toml` 增加可选依赖 `[project.optional-dependencies] gui = ["PySide6>=6.6"]`（dev 组增加 `pyinstaller`），增加 `[project.scripts] safecopy-gui = "transfertools.gui.main:main"`；验证：`uv sync --extra gui` 成功且 `uv sync`（不带 extra）后的环境运行 CLI 测试仍全绿
- [x] 2.2 新增 `src/transfertools/gui/__init__.py` 与 `__main__.py`（`python -m transfertools.gui` 入口）；GUI 入口对 `ImportError` 捕获并打印中文安装提示、以非零退出码结束；验证：不带 gui extras 的环境执行 GUI 入口输出提示且退出码非零、无未处理 traceback

## 3. GUI 编排层（不依赖 Qt）

- [x] 3.1 新建 `transfertools/gui/controller.py`：纯 Python 控制器——预检（源存在性、目录源需递归、递归模式目标不得等于/位于源目录内部，复用或抽取 `cli._preflight` 等价逻辑保证与 CLI 语义一致）、把源列表展开为 `(src, dst)` 任务列表（含递归遍历与总字节统计，统计失败降级为仅按文件数计进度）、逐任务调用 `copy_file` 并通过回调上报（文件开始/字节进度/日志行/错误/汇总）；单任务失败不中断后续任务；验证：控制器单元测试覆盖预检各类拒绝、多源展开、失败汇总、取消传播
- [x] 3.2 控制器取消协议：`request_cancel()` 置位 `threading.Event`，正在复制的文件在块边界停止、半成品被引擎删除、剩余任务不再开始，最终汇总状态为「已取消」；验证：单元测试模拟大文件复制中途取消，断言半成品不存在且状态为 cancelled

## 4. 工作线程与信号桥接

- [x] 4.1 新建 `transfertools/gui/worker.py`：`QObject` worker 在 `QThread` 中运行控制器，将回调转换为 Qt 信号（`bytesProgress(int,int)`、`fileStarted(str)`、`logLine(str)`、`fileError(str,str,str)`、`finished(summary)`）；取消按钮经线程安全方式调用 `request_cancel()`；验证：offscreen 测试驱动 worker 完成一次真实小文件复制并断言信号序列

## 5. 主窗口视图（D-g2 三段式，中文文案）

- [x] 5.1 新建 `transfertools/gui/main_window.py`：装配三段式布局——源列表区（`QListWidget` + 添加文件/添加文件夹/移除选中/清空按钮，接受拖放并去重）、目标与选项区（目标目录输入 + 浏览、递归复选框、块大小下拉 1/2/4/8 MiB 默认 4、no-clobber 与 no-preserve-metadata 复选框）、进度与日志区（总进度条、当前文件标签、只读日志、「开始」「取消」按钮）；全部文案中文；验证：`QT_QPA_PLATFORM=offscreen` 下 UI 测试断言控件存在、初始状态正确（源为空或目标未设时「开始」禁用、非运行期「取消」禁用）
- [x] 5.2 状态机接线：running 期间锁定输入控件仅「取消」可用；`fileError` 弹 `QMessageBox.critical` 并写日志、不中断队列；finished 后展示成功/失败汇总并恢复 idle；验证：offscreen UI 测试覆盖「开始→完成」「开始→取消→可再次开始」「单文件失败弹对话框且其余文件继续」三条流程（临时目录真实复制）

## 6. 打包链路（D-g4）

- [x] 6.1 新增 `packaging/safecopy-gui.spec`（onefile、windowed、name=safecopy-gui、version resource、icon）与占位图标 `packaging/assets/safecopy-gui.ico`（脚本生成，README 注明可替换）；验证：Linux 宿主以 `pyinstaller --onedir` 冒烟构建成功且产物可启动（`--help` 级冒烟），`.spec` 分析阶段无缺失模块告警
- [x] 6.2 新增 Windows 本机打包脚本 `packaging/build-exe.ps1`（建 venv → 安装 `.[gui]` 与 pyinstaller → 按 spec 构建 → 校验 `dist/safecopy-gui.exe` 存在）；验证：脚本静态审查 + README 记录使用方式（Linux 宿主无法执行，标注由 Windows 本机或 CI 验证）
- [x] 6.3 新增 `.github/workflows/build-exe.yml`：windows-latest，tag 与 workflow_dispatch 触发，setup-python 3.13 → 安装依赖 → 运行 pytest 套件 → pyinstaller 构建 → upload-artifact 上传 `safecopy-gui.exe`；验证：工作流 YAML 语法校验通过（如 `python -c "yaml.safe_load(...)"`），并在 README 说明 artifact 获取方式

## 7. 文档与整体验收（D-g5）

- [x] 7.1 更新 `README.md`：GUI 使用说明（中文）、`uv sync --extra gui` 安装方式、PySide6 LGPL 许可与 Qt 库替换合规说明、打包方式（CI artifact / Windows 本机脚本）、以及「Linux 宿主不产出 Windows exe」的边界声明；验证：文档渲染检查、链接可达
- [x] 7.2 整体验收：`QT_QPA_PLATFORM=offscreen pytest` 全套测试（含既有 copier/cli/no_copy_api 与新增 gui 测试）全绿；Linux onedir 冒烟构建通过；`openspec validate add-safecopy-gui --strict` 通过；验证：上述三条命令均退出码 0
