# safe-file-copy Specification

## Purpose
提供一种不依赖操作系统级直接复制 API 的文件复制能力：在目标（典型为外部存储）路径上先创建目标文件，再以分块方式顺序写入源数据，经 SHA-256 完整性校验后交付，从而避开安全软件对复制 API 的拦截。

## Requirements

### Requirement: 分块写入式文件复制

系统 SHALL 在不调用任何直接复制 API（如 `shutil.copy*`、`os.sendfile`、`CopyFile` 等平台复制原语）的前提下，将源文件内容复制到目标路径：先在目标路径创建目标文件（目标已存在时 MUST 截断或按用户选项处理），再以分块方式（默认块大小在 1~8 MiB 范围内，可配置）顺序读取源文件并写入目标文件。

#### Scenario: 单文件复制成功

- **WHEN** 用户执行 `safecopy <src> <dst>`，源文件可读且目标路径可写
- **THEN** 目标路径出现一个与源文件字节内容完全一致的新文件，命令以退出码 0 结束

#### Scenario: 复制过程不调用直接复制 API

- **WHEN** 复制任意文件
- **THEN** 实现仅通过普通文件打开/读/写系统调用完成数据传输，不调用 `shutil.copy`、`shutil.copy2`、`shutil.copyfile`、`os.sendfile` 或平台等价的复制原语

#### Scenario: 目标文件已存在

- **WHEN** 目标路径已存在同名文件且用户未指定保留选项
- **THEN** 系统 SHALL 截断并覆盖该文件，或按 CLI 选项（如 `--no-clobber`）报错跳过；行为 MUST 在帮助文档中说明

### Requirement: 完整性校验

系统在写入完成后 MUST 先 flush 并 fsync 目标文件，再分别对源文件与目标文件计算 SHA-256 摘要并比对；只有两者一致时才视为复制成功。

#### Scenario: 校验一致

- **WHEN** 分块写入与 fsync 完成，源文件与目标文件的 SHA-256 一致
- **THEN** 命令报告成功并以退出码 0 结束

#### Scenario: 校验不一致

- **WHEN** 源文件与目标文件的 SHA-256 摘要不一致
- **THEN** 命令 MUST 报告校验失败、以非零退出码结束，并删除目标上的半成品文件

#### Scenario: 写入中途失败

- **WHEN** 分块写入或 fsync 过程中发生 I/O 错误（如外部存储被拔出、空间不足）
- **THEN** 命令 MUST 以非零退出码结束并尽力删除目标上的半成品文件，不留未校验的残文件

### Requirement: 元数据保留

系统在完整性校验通过后 SHALL 将源文件的权限位与时间戳（atime/mtime）保留到目标文件；ACL、扩展属性（xattr）、owner 不在保留范围内。目标文件系统不支持保留操作时 MUST 降级为仅警告，复制本身仍视为成功。用户 SHALL 可通过 CLI 选项（如 `--no-preserve-metadata`）关闭该行为。校验失败时 MUST NOT 保留元数据——半成品文件直接删除。

#### Scenario: 默认保留权限与时间戳

- **WHEN** 复制成功且未指定关闭选项
- **THEN** 目标文件的权限位与 mtime SHALL 与源文件一致

#### Scenario: 文件系统不支持时降级

- **WHEN** 目标文件系统不支持 chmod/utime（如 FAT32/exFAT 外部存储）
- **THEN** 系统 SHALL 打印警告并继续，复制结果仍判定为成功，并指示元数据未保留

#### Scenario: 用户关闭元数据保留

- **WHEN** 用户指定 `--no-preserve-metadata`
- **THEN** 系统 SHALL 跳过权限位与时间戳的保留，仅保证字节内容一致

#### Scenario: 校验失败不保留元数据

- **WHEN** SHA-256 校验不一致
- **THEN** 系统 MUST 删除半成品目标文件，不在其上执行任何元数据保留操作

### Requirement: 多文件复制 CLI

系统 SHALL 提供命令行接口，支持一次指定多个源文件复制到一个目标目录。

#### Scenario: 多源复制到目录

- **WHEN** 用户执行 `safecopy <src1> <src2> ... <dir>`，最后一个参数为已存在的目录
- **THEN** 每个源文件 SHALL 按单文件流程逐一复制到该目录下（保留原文件名），任一文件失败 MUST 报告该文件错误但不影响其余文件的处理，最终以非零退出码汇总指示存在失败

#### Scenario: 单源复制到目录

- **WHEN** 用户执行 `safecopy <src> <dir>`，目标为已存在目录
- **THEN** 系统 SHALL 将源文件复制为 `<dir>/<源文件名>`

#### Scenario: 参数错误

- **WHEN** 用户提供的源路径不存在，或多源模式下最后一个参数不是目录
- **THEN** 命令 MUST 打印明确的错误信息并以非零退出码结束，不产生任何目标文件

### Requirement: 递归目录复制（可选）

系统 SHALL 支持可选的递归目录复制模式：用户显式启用（如 `-r`/`--recursive`）时，将源目录树复制到目标目录，保持相对目录结构；未启用时传入目录作为源 MUST 报错。

#### Scenario: 递归复制目录树

- **WHEN** 用户执行 `safecopy -r <srcdir> <dstdir>`
- **THEN** 源目录下所有文件 SHALL 按相对路径逐一以分块写入方式复制到目标目录，每个文件均执行完整性校验

#### Scenario: 未启用递归时传入目录

- **WHEN** 用户未指定递归选项而源参数为目录
- **THEN** 命令 MUST 报错提示需要递归选项，并以非零退出码结束
