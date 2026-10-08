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
uv run pytest -q
```

覆盖分块边界（0 字节、恰好 1 块、1 块 ±1 字节、多块）、目标已存在/截断覆盖、
SHA-256 不一致、写入中途失败与 Ctrl-C 中断的清理、预分配降级、元数据保留降级、
CLI 参数错误、多源部分失败（退出码 2）与递归目录树复制。

## OpenSpec

本能力由 OpenSpec change `add-safe-file-copy` 驱动，规划工件位于
`openspec/changes/add-safe-file-copy/`（proposal / spec delta / design / tasks）：

```bash
openspec validate add-safe-file-copy
```
