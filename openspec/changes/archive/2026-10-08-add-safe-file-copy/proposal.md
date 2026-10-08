# Proposal

## Why

在安全软件（EDR/杀毒）管控的环境中，直接调用操作系统级复制 API（如 Windows `CopyFile`、Python `shutil.copy`）复制文件到外部存储（U 盘、移动硬盘、网络盘）时，常被行为审计规则拦截或告警，导致复制失败。需要一个不触发复制 API 钩子的替代方案：以「创建目标文件 + 分块顺序写入」的普通读写方式完成等效的复制，并保证数据完整性可验证。

## What Changes

- 新增一个命令行文件复制工具 `safecopy`：将一个或多个源文件复制到目标路径（典型场景为外部存储）。
- 复制实现不调用任何直接复制 API（`shutil.copy*`、`os.sendfile`、`CopyFile` 等），改为：在目标路径创建（可选预分配/截断）目标文件，然后以 1~8 MiB 分块顺序读取源文件并写入目标文件。
- 写入完成后执行 flush + fsync，再对源文件与目标文件分别计算 SHA-256 并比对，不一致时报错并清理半成品目标文件。
- 复制成功（校验通过）后默认保留源文件的权限位与时间戳，`--no-preserve-metadata` 可关闭；ACL/xattr/owner 不在范围，保留失败降级为仅警告。
- CLI 支持单文件与多文件复制（多源到目标目录）；递归目录复制作为可选能力纳入任务范围。
- 提供清晰的退出码与错误信息，便于脚本化调用与人工排查。

## Capabilities

### New Capabilities

- `safe-file-copy`: 以「先创建目标文件、再分块写入、最后哈希校验」的方式将文件复制到目标（外部存储）路径，避开直接复制 API 被安全软件拦截的问题；涵盖 CLI 接口、分块复制流程、完整性校验与失败清理行为。

### Modified Capabilities

（无——本项目尚无既有 specs。）

## Impact

- **代码**：新增 CLI 入口与复制核心模块（纯 Python 标准库实现，无新增第三方依赖）；现有 `main.py` 占位脚本可保留或由后续 change 处理。
- **API**：新增命令行接口 `safecopy <src...> <dst>`（通过 `uv run` 或安装后的 console script 调用）。
- **依赖**：仅使用 Python 标准库（`argparse`、`hashlib`、`os`、`pathlib` 等），`pyproject.toml` 需增加 `[project.scripts]` 入口。
- **系统**：面向 Windows/Linux 桌面环境，目标路径多为外部存储（USB/移动硬盘/网络挂载）；fsync 行为在外部存储上可能较慢，属预期代价。
