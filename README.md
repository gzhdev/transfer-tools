# transfertools / safecopy

以「**先创建目标文件 → 分块顺序写入 → flush + fsync → SHA-256 双端校验**」的方式复制文件，
不调用任何直接复制 API，用于绕过安全软件（EDR/杀毒）对复制语义调用的行为拦截。

在安全软件管控的环境中，复制文件到外部存储（U 盘、移动硬盘、网络盘）时，
`shutil.copy*` / `CopyFile` / `sendfile` / `copy_file_range` 这类复制原语常被行为审计规则
拦截或告警。本工具改走与编辑器写文件同形的普通 `open` / `read` / `write` 路径，
并保证数据完整性可验证。

## 安装与运行

项目用 [uv](https://docs.astral.sh/uv/) 管理，Python ≥ 3.13，运行时**零第三方依赖**。

```bash
uv run safecopy --help            # 直接运行 CLI
uv run python -m transfertools    # 等价的模块入口
```

也可以通过 `[project.scripts]` 安装后作为 `safecopy` 命令使用（`uv sync` 后位于 `.venv/bin/safecopy`）。

## 用法

```
safecopy [-r] [--chunk-size MIB] [--no-clobber] [--no-preserve-metadata] <src...> <dst>
```

| 参数 | 说明 |
| --- | --- |
| `<src...>` | 一个或多个源文件；启用 `-r` 时只能是**唯一**的源目录 |
| `<dst>` | 目标路径。已是目录时复制为 `<dst>/<源文件名>`；多源复制要求它必须是**已存在目录** |
| `-r`, `--recursive` | 递归复制源目录树到目标目录，按相对路径重建结构（含空目录） |
| `--chunk-size MIB` | 分块写入的块大小，单位 MiB，**范围 1~8，默认 4** |
| `--no-clobber` | 目标已存在时报错跳过，不截断覆盖（默认行为是**截断覆盖**） |
| `--no-preserve-metadata` | 不保留源文件的权限与时间戳 |

示例：

```bash
# 单文件 → 指定文件名（目标已存在则截断覆盖）
uv run safecopy ./report.pdf /media/usb/report.pdf

# 单文件 → 目录（落到 <dir>/<源文件名>）
uv run safecopy ./report.pdf /media/usb/

# 多文件 → 已存在目录（逐文件处理、逐文件报错、不中断其余文件）
uv run safecopy ./a.bin ./b.bin /media/usb/

# 递归复制目录树，1 MiB 块
uv run safecopy -r --chunk-size 1 ./photos /media/usb/photos

# 不覆盖外部存储上已存在的文件
uv run safecopy --no-clobber ./a.bin /media/usb/
```

成功时 stdout 输出一行 `完成: <目标>  大小=<字节>  sha256=<摘要>`；
写入/校验的阶段提示（`写入中` / `校验中`）与所有错误、警告都走 stderr，便于脚本只消费 stdout。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部文件复制完成且 SHA-256 校验通过 |
| `1` | 参数或用法错误：源不存在、未启用 `-r` 却传入目录、多源目标不是目录、`--chunk-size` 越界、缺少参数、递归目标落在源目录内等；**不会产生任何目标文件** |
| `2` | 存在复制失败（源与目标为同一文件、写入 I/O 错误、校验不一致、`--no-clobber` 命中已存在目标）；其余文件仍会继续处理 |

## 图形界面（safecopy-gui）

面向 Windows 10+ 桌面用户的可选前端（PySide6）。它复用与 CLI **完全相同**的复制引擎
（先创建目标文件 → 分块顺序写入 → `flush` + `fsync` → SHA-256 双端校验 → 失败清理），
只是把源收集、选项、进度与错误处理搬到了窗口里。

安装（GUI 是**可选 extras**，CLI 运行时不新增任何依赖）：

```bash
uv sync --extra gui                      # 源码开发
pip install "transfertools[gui]"         # 安装为命令
```

启动：

```bash
uv run safecopy-gui                      # 控制台脚本入口
uv run python -m transfertools.gui       # 等价的模块入口
```

未安装 PySide6 时，GUI 入口会打印中文安装提示并以退出码 `3` 结束（不会抛未处理的导入堆栈），
`safecopy` CLI 则照常工作。

### 界面与选项

单窗口三段式布局，全部文案为中文：

1. **源文件 / 文件夹**：列表 +「添加文件…」（支持多选）/「添加文件夹…」/「移除选中」/「清空」；
   也可把文件或文件夹直接**拖入列表**。重复路径自动去重（按规范化绝对路径判定，
   `./a` 与 `a`、符号链接指向同一文件同样只保留一条）。
2. **目标目录与复制选项**：目标目录（浏览或输入，必须是**已存在目录**）、递归开关、
   块大小 1/2/4/8 MiB（默认 4）、「目标已存在时跳过」、「不保留权限与时间戳」。
   复制进行中这些控件全部锁定，只有「取消」可用。
3. **进度与日志**：总进度条（按字节计；源大小无法统计时自动降级为按文件数）、当前文件与已写入字节、
   只读日志（写入中 / 校验中 / 完成 / 失败原因）、状态汇总，以及底部「开始」「取消」。

选项与 CLI 一一对应：

| GUI 控件 | CLI 对应 |
| --- | --- |
| 递归复制目录 | `-r` / `--recursive` |
| 块大小（1/2/4/8 MiB，默认 4） | `--chunk-size MIB` |
| 目标已存在时跳过 | `--no-clobber` |
| 不保留权限与时间戳 | `--no-preserve-metadata` |

### 行为与错误处理

- **单文件失败不中断队列**：与 CLI 一致——失败文件弹错误对话框并在日志区留一条记录，其余文件继续复制。
- **参数性错误在开始前一次性报出**：源失效、源含目录却未勾选递归、目标目录不存在或不是目录、
  递归目标位于源目录内部（自复制）等，都在动手前提示，**不产生任何目标文件**。
- **取消即清理**：点「取消」后当前文件在下一个块边界停止，其半成品目标文件由引擎既有的失败清理路径
  （与 CLI 同一套 D4 语义）删除，剩余文件不再开始；界面回到可再次开始的状态。
- **复制不阻塞界面**：复制在 `QThread` 中执行，进度/日志经 Qt 信号投递到界面线程；日志区在复制期间仍可滚动。

GUI 退出码：`0` 正常退出 / `1` 用法错误 / `2` 启动期错误 / `3` 缺少 GUI 依赖（未安装 PySide6）。
复制结果不通过退出码表达，而是显示在状态栏与日志区；CLI 的 `0/1/2` 语义不变。

### 无头自检

打包产物或 CI 可在没有显示器的环境下用 `--self-test` 构造一次界面：

```bash
QT_QPA_PLATFORM=offscreen uv run safecopy-gui --self-test
# 自检通过：safecopy-gui 0.1.0 / Qt 6.x / 窗口标题「…」/ 文案均为中文。
```

## 打包与分发（safecopy-gui.exe）

产物是 **PyInstaller onefile + windowed** 的单文件 `safecopy-gui.exe`：无控制台窗口，
内嵌版本资源（`packaging/version_info.txt`）与图标（`packaging/assets/safecopy-gui.ico`）。

### CI 自动构建（推荐）

`.github/workflows/build-exe.yml` 在 `windows-latest` 上执行：安装 `.[gui]` 与 PyInstaller →
运行完整测试套件（`QT_QPA_PLATFORM=offscreen`）→ 按 `packaging/safecopy-gui.spec` 构建 →
校验并上传 artifact。

- **触发**：推送 `v*` 标签，或在 Actions 页面手动 `workflow_dispatch`；
- **获取**：在 workflow 运行页面的 **Artifacts** 区下载 `safecopy-gui-windows`（内含 `safecopy-gui.exe`）。

### Windows 本机一键构建

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1
```

流程：创建/复用 `.venv-win` → 安装 `.[gui]` 与 pyinstaller → 运行测试 → 构建 →
校验 `dist\safecopy-gui.exe` 存在。可选参数：`-SkipTests`（跳过测试）、`-Clean`（先清理 `build/`、`dist/`）、
`-VenvDir`、`-PythonLauncher`。产出与 CI 等价。

### Linux 宿主只能做替代验收

PyInstaller **不支持交叉编译**，Linux 宿主无法产出 Windows exe。因此 Linux 上的验收是：

```bash
bash packaging/build-linux-smoke.sh
```

它用同一份 `.spec` 以 `SAFECOPY_GUI_PACKAGE_MODE=onedir` 构建 **onedir** 目录形态
（与 onefile 共享「分析 + 收集」阶段，足以暴露漏打包、缺 Qt 插件等问题），再执行产物的
`--help` 与 `--self-test` 自检。**Windows 单文件 exe 的实际产出与验收由 CI（windows-latest）
或 Windows 本机脚本完成**，这是本仓库的既定边界（design D-g5）。

### 图标与版本资源

- 图标：`packaging/assets/safecopy-gui.ico`，当前是 `python packaging/make_icon.py` 生成的**占位图标**
  （多尺寸 BMP ICO）。替换正式图标时直接覆盖该文件即可，打包配置无需改动。
- 版本资源：`packaging/version_info.txt`（Windows 文件属性中的产品名/版本/版权），版本号与
  `pyproject.toml` 保持一致（`tests/test_packaging_config.py` 会校验）。
- exe **未做代码签名**，首次运行可能被 SmartScreen 拦截（可选「更多信息 → 仍要运行」）。

### 许可与合规（PySide6 / Qt）

- GUI 使用 **PySide6（Qt for Python）**，其许可为 **LGPL v3**；CLI 不使用 Qt，因此不受影响。
  本仓库代码未附带 LICENSE 文件，再分发前请先确定自身代码的许可。
- onefile 会把 Qt 动态库打进 exe。LGPL 要求使用者能替换这些库，因此再分发时建议：
  1. 随 exe 提供 LGPL v3 文本与所用 Qt/PySide6 版本说明（可用 `pip show pyside6` 获取）；
  2. 提供替换途径——本仓库是源码分发，任何人都可以用 `packaging/build-exe.ps1` 或
     `packaging/safecopy-gui.spec` 重新链接打包；对替换要求更严格的场景可改用 **onedir** 形态
     （`SAFECOPY_GUI_PACKAGE_MODE=onedir`）分发，让 Qt 库以独立文件形式可见、可替换；
  3. 若修改了 Qt/PySide6 自身的源码，须一并提供修改后的源码。
- 以上为工程实践说明，不构成法律意见；正式发布前请按目标地区的合规要求复核。

## 行为细节

- **同源防护**：在打开目标文件**之前**先做同源检测——目标已存在时按 `(st_dev, st_ino)`
  （`os.path.samefile`）比对，因此硬链接、符号链接、大小写不敏感文件系统都能识别；目标尚不存在时
  比较规范化绝对路径，`./f` 与 `f`、`dir/../f` 与 `f` 这类写法同样被判定为同一文件。命中即报错退出
  （退出码 2）且**源文件零改动**，绝不先截断再报错。递归模式下目标目录等于源目录或位于源目录内部
  （`safecopy -r dir dir`、`safecopy -r dir dir/sub`）属用法错误（退出码 1），直接拒绝、不产生任何写入。
- **不调用复制 API**：实现只有普通文件 I/O（`open`/`read`/`write`/`flush`/`os.fsync`），
  不 import 也不调用 `shutil.copy*`、`os.sendfile`、`os.copy_file_range`、`CopyFile` 等；
  `tests/test_no_copy_api.py` 用 AST 静态分析 + 运行时探针双重断言防回归。
- **完整性校验**：写入并 `flush` + `fsync` 之后，重新打开源与目标文件分别流式计算 SHA-256
  并比对；不一致时报错并删除目标上的半成品文件。校验失败信息会提示「源文件可能在复制过程中被修改」。
  顺序上先 `fsync` 再校验，避免只读到页缓存、写回失败却在命令返回成功后才暴露。
- **失败清理**：写入 / fsync / 校验任一环节失败（含 Ctrl-C 中断）都会尽力删除目标上的半成品，
  不留未校验的残文件；删除失败只警告，不掩盖原始错误。
- **既定行为（对齐设计 D1「先创建目标文件」）**：目标文件先于源数据被创建/截断，因此当源不可读
  （权限不足、源已被删除等）或读取中途失败时，目标位置上**原本已存在的同名文件会先被截断、随后在
  失败清理中被删除**。若该目标文件必须原样保留，请先确认源可读，或改用 `--no-clobber` 让工具在目标
  已存在时直接跳过。
- **预分配**：POSIX 上尽力用 `os.posix_fallocate` 预分配目标文件，减少外部存储碎片与中途空间不足的
  概率；文件系统不支持（如 FAT32/exFAT）或其他平台则降级为普通截断创建并**仅警告**。
- **元数据**：默认在**校验通过后**保留源文件的权限位与时间戳，目标文件系统不支持时降级并仅警告。
- **块大小**：默认 4 MiB，可在 1~8 MiB 内调整；小于 1 MiB 会显著增加系统调用次数，
  大于 8 MiB 吞吐收益趋平而内存占用上升。
- **性能提示**：外部存储上 `fsync` 可能耗时到秒级，命令看起来「卡住」属正常现象。

## 限制与免责

- 本工具只规避**复制 API 钩子**，不保证规避安全软件对「大量写入外部存储」等行为审计的策略。
- 不做断点续传、并发复制；符号链接按普通文件内容复制（不保留链接本身）。
- 外部存储写回失败、坏块等硬件问题只能被校验发现，不能被避免。

## 测试

```bash
uv run pytest -q                                        # 全量（含 GUI 界面测试）
QT_QPA_PLATFORM=offscreen uv run pytest -q tests/test_gui_main_window.py   # 只跑界面测试
```

覆盖分块边界（0 字节、恰好 1 块、1 块 ±1 字节、多块）、目标已存在/截断覆盖、
SHA-256 不一致、写入中途失败与 Ctrl-C 中断的清理、预分配降级、元数据保留降级、
CLI 参数错误、多源部分失败（退出码 2）与递归目录树复制。

GUI 部分（`tests/conftest.py` 强制 `QT_QPA_PLATFORM=offscreen`，无需显示器）覆盖：

- `tests/test_gui_controller.py`：预检各类拒绝（与 CLI 文案逐字一致）、多源展开、总字节统计降级、
  逐文件容错、`no-clobber` 计数、选项→引擎参数映射、取消协议（半成品删除、剩余不开始）；
- `tests/test_gui_worker.py`：`QThread` 中真实复制并断言信号序列、预检失败信号、跨线程取消；
- `tests/test_gui_main_window.py`：三段式布局与中文文案、按钮/对话框/拖拽（注入真实 `QDropEvent`）去重、
  选项映射与运行期锁定、真实复制的进度与日志、错误对话框双通道、取消后恢复可再次开始；
- `tests/test_gui_entrypoint.py`：`--help` / `--version` / `--self-test` / 用法错误退出码、
  缺少 PySide6 时的中文提示，以及「CLI 不导入 Qt」的回归断言；
- `tests/test_packaging_config.py`：`.spec`（onefile + windowed + onedir 开关）、图标与版本资源、
  Windows 打包脚本、CI 工作流 YAML 与 README 交付说明的静态校验。

## OpenSpec

本仓库的能力由 OpenSpec change 驱动，规划工件位于 `openspec/changes/`：

| change | 内容 |
| --- | --- |
| `add-safe-file-copy`（已归档） | 分块写入式复制引擎与 CLI |
| `add-safecopy-gui` | PySide6 图形界面与 Windows 打包交付 |

```bash
openspec validate add-safecopy-gui --strict
```
