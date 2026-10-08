# Design

## Context

项目是 uv 管理的 Python ≥3.13 应用（`transfertools`），当前仅有占位 `main.py`，无任何第三方依赖。动机见 proposal.md「Why」：安全软件通常 hook 的是复制语义 API（`CopyFile`/`shutil.copy`/`sendfile`），而普通的 open/read/write 序列不在其拦截规则内。本设计的全部决策围绕「用普通读写完成等效复制，并保证完整性」展开。

## Goals / Non-Goals

**Goals:**

- 纯 Python 标准库实现，跨 Windows / Linux 运行，目标路径典型为外部存储（USB、移动硬盘、网络挂载）。
- 复制路径上只出现普通文件 I/O：`open(src, 'rb')` → 分块 `read` → `open(dst, 'wb')` → 分块 `write` → `flush` + `os.fsync`。
- 默认块大小 4 MiB（落在 1~8 MiB 建议区间），CLI 可用 `--chunk-size` 在 1~8 MiB 内调整。
- SHA-256 双端比对校验；失败路径保证目标不残留未校验文件。
- 可选目标文件预分配/截断，减少外部存储上的碎片与中途空间不足的概率。

**Non-Goals:**

- 元数据保留范围收敛为「仅保留权限位与时间戳」（见 D7）：ACL、扩展属性（xattr）、owner 不在范围内；保留失败降级为仅警告，不影响复制成功判定。
- 不做断点续传、多线程/异步并发复制、进度条以外的交互 UI。
- 不处理符号链接跟随策略之外的特殊文件（设备文件、管道、硬链接保持）。
- 递归目录复制为可选任务，未实现时不阻塞单/多文件功能交付。

## Decisions

### D1: 禁用直接复制 API，采用「创建 + 分块顺序写」

核心路径伪代码：

```
src_f = open(src, 'rb')
dst_f = open(dst, 'wb')          # 创建或截断目标
可选: os.posix_fallocate / SetEndOfFile 预分配
while chunk := src_f.read(chunk_size):
    dst_f.write(chunk)
dst_f.flush(); os.fsync(dst_f.fileno())
```

- **为什么**：`shutil.copy*` 在 POSIX 上会退化到 `sendfile`/`copy_file_range`，在 Windows 上走 `CopyFileExW`，均为安全软件重点 hook 对象；普通 `read`/`write` 与任何编辑器/工具写文件的行为同形，不触发复制审计规则。同时这也写入代码规范：实现中 MUST NOT import 上述 API，review 时检查。
- **备选**：`mmap` 读写（同样普通 I/O，但外部存储上错误处理复杂、部分平台语义不一致）——放弃，作为后续优化备选。

### D2: 块大小默认 4 MiB，区间 1~8 MiB

- **为什么**：小于 1 MiB 时系统调用次数过多，外部存储（尤其 USB 2.0 / SMR 移动盘）吞吐明显下降；大于 8 MiB 内存占用上升但吞吐收益趋平，且便携机上不必要。4 MiB 是常见文件系统簇/写入缓冲的友好倍数。
- **备选**：固定 1 MiB（保守但偏慢）；自动探测（复杂且收益不明确）——放弃。

### D3: 写后 fsync，再双端 SHA-256 比对

- **为什么**：不 fsync 时，校验读到的可能仍是页缓存，写回失败（拔盘、坏块）会在命令返回成功后才暴露。顺序为：写入 → `flush` → `fsync` → 重新打开双方文件流式计算 SHA-256（同样分块，复用 chunk 读循环）→ 比对。
- **代价**：外部存储上 fsync 可能很慢（秒级），属可接受的安全代价；提供 `--no-verify` 不在本 change 范围，保持默认最安全行为。
- **备选**：写的同时增量计算源端哈希、目标端写后重读哈希（少一遍源文件 I/O）——源文件在写入期间被第三方修改会造成误判，双端事后重算语义更清晰，选后者。

### D4: 失败清理策略

- 写入错误或校验失败 → 关闭句柄后 `os.remove(dst)`；删除失败仅警告不掩盖原始错误。
- 不做「临时文件 + rename」方案：rename 到最终名之前文件已在外部存储上完整写入，固然更安全，但 spec 要求的行为就是「目标路径先创建」，且临时文件方案会在安全软件视角引入额外的 rename 审计点；半成品直接删除即可。
- **备选**：保留半成品便于续传——与 Non-Goals（无断点续传）冲突，放弃。

### D5: CLI 结构与退出码

- `pyproject.toml` 增加 `[project.scripts] safecopy = "transfertools.cli:main"`；亦支持 `uv run python -m transfertools`。
- 参数：`safecopy [-r] [--chunk-size MiB] [--no-clobber] <src...> <dst>`。`argparse` 实现，`nargs='+'` 收取源列表，最后一个位置参数为目标。
- 退出码：`0` 全部成功；`1` 参数/用法错误；`2` 部分或全部复制失败（含校验失败）。多文件模式逐文件处理、逐文件报错、最后汇总。

### D6: 包布局

```
src/transfertools/__init__.py
src/transfertools/__main__.py   # python -m 入口
src/transfertools/cli.py        # argparse 与编排
src/transfertools/copier.py     # 分块复制、fsync、校验、清理
tests/                          # pytest（新增 dev 依赖 pytest）
```

- **为什么**：现项目为 uv 默认「应用布局」（根目录 `main.py`，无 `[build-system]`）。本 change 顺带改为 `src` 布局 + hatchling 构建后端，使 console script 可安装；这是最小可行改造（文件极少）。

### D7: 保留权限与时间戳（实施阶段补入范围）

- **背景**：实施阶段明确的交付流水线为「创建目标文件 → 分块写入 → fsync → SHA-256 校验 →
  保留权限/时间戳 → 失败清理」，与原 Non-Goal「不复制文件元数据」冲突，故收敛为
  「仅保留权限位与时间戳」，ACL / 扩展属性 / owner 仍不在范围。本条为实施阶段补记的决策，
  并已同步补入 specs delta（「元数据保留」需求）与 tasks.md（任务 2.5）。
- **做法**：**校验通过之后**再执行 `os.chmod`（取 `stat.S_IMODE`）与 `os.utime`（atime/mtime）；
  校验失败时直接删除半成品，绝不在待删除文件上留下已保留的元数据。
- **降级**：目标文件系统不支持时（外部存储上的 FAT32/exFAT 常见）仅警告，并把
  `CopyResult.metadata_preserved` 置为 `False`，复制本身仍视为成功；CLI 提供
  `--no-preserve-metadata` 关闭该行为。
- **备选**：沿用原 Non-Goal 不做元数据——与实施任务描述冲突，放弃。

## Risks / Trade-offs

- [外部存储 fsync 极慢，用户误以为卡死] → 默认打印每文件的进度/阶段提示（写入中/校验中），文档说明 fsync 代价。
- [源文件在复制期间被修改导致校验必然失败] → 校验失败错误信息中明确提示「源文件可能在复制过程中被修改」，用户重试即可；不做文件锁（跨平台不可靠）。
- [某些安全软件同样审计大量写外部存储的行为] → 缓解不在工具能力内；文档说明本工具只规避复制 API hook，不保证规避所有行为审计。
- [预分配在不支持的文件系统上失败（如 FAT32 上的 fallocate）] → 预分配失败降级为普通截断创建，仅警告。
- [改用 src 布局 + 构建后端会变动 pyproject.toml] → 变更极小且向后兼容（`uv run` 不受影响），在 tasks 中列为独立步骤。

## Migration Plan

纯新增功能，无存量数据/接口迁移。实施顺序见 tasks.md；回滚即删除新增包与入口即可，无状态残留。

## Open Questions

- Windows 上预分配的等价调用（`SetEndOfFile` 经 `msvcrt`/`ctypes`）是否纳入本 change，或首版仅在 POSIX 做 `posix_fallocate`、Windows 直接跳过——可在实现任务中以「尽力预分配、失败降级」落地，不影响 spec。
