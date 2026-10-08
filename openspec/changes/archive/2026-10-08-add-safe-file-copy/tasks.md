# Tasks

## 1. 项目布局与打包改造

- [x] 1.1 将项目改为 src 布局：新建 `src/transfertools/` 包（`__init__.py`），在 `pyproject.toml` 增加 `[build-system]`（hatchling）与 `[project.scripts] safecopy = "transfertools.cli:main"`；验证 `uv run safecopy --help` 能进入 CLI（参数未实现时可先打印用法）
- [x] 1.2 添加开发依赖 pytest（`uv add --dev pytest`）并新建 `tests/`；验证 `uv run pytest` 可运行（允许 0 用例通过）

## 2. 复制核心模块（copier）

- [x] 2.1 实现 `src/transfertools/copier.py` 的分块复制函数：`open(src,'rb')` → 创建/截断目标 `open(dst,'wb')` → 按 chunk_size（默认 4 MiB，允许 1~8 MiB）循环 `read`/`write` → `flush` + `os.fsync`；全程禁止 import 或使用 `shutil.copy*`、`os.sendfile`、`os.copy_file_range`（可用 grep 验证无这些调用）
- [x] 2.2 实现可选预分配：POSIX 尝试 `os.posix_fallocate`，其他平台或不支持时降级为普通截断创建并仅警告；验证在 Linux 上对临时目录复制正常，且禁用预分配路径也可工作
- [x] 2.3 实现完整性校验：写入+fsync 后重新打开源与目标文件，分块计算 SHA-256 并比对；不一致时抛专用异常；验证单元测试：正常复制哈希一致通过，人为篡改目标后触发校验失败
- [x] 2.4 实现失败清理：写入/fsync/校验任一失败时关闭句柄并 `os.remove(dst)`，删除失败仅警告；验证单元测试：模拟写失败与校验失败后目标路径不存在半成品文件
- [x] 2.5 实现元数据保留（D7）：校验通过后 `os.chmod`（`stat.S_IMODE`）+ `os.utime` 保留权限位与 atime/mtime，目标文件系统不支持时降级为仅警告且 `metadata_preserved=False`，校验失败时不执行保留；验证单元测试覆盖默认保留、降级警告与关闭开关（CLI `--no-preserve-metadata` 见 3.1）

## 3. CLI（cli 模块与入口）

- [x] 3.1 实现 `src/transfertools/cli.py`：`argparse` 解析 `[-r/--recursive] [--chunk-size MiB] [--no-clobber] [--no-preserve-metadata] <src...> <dst>`，含 `--chunk-size` 范围校验（1~8）；验证 `--help` 输出与非法 chunk-size 报错
- [x] 3.2 实现单文件复制编排：目标为目录时落到 `<dir>/<源文件名>`；`--no-clobber` 时目标已存在则报错跳过；验证对应 spec 场景的集成测试通过
- [x] 3.3 实现多文件复制编排：多源时目标必须是已存在目录，逐文件复制、逐文件报错、不中断其余文件；退出码 `0` 全成功 / `1` 用法错误 / `2` 存在失败；验证集成测试覆盖参数错误、部分失败（退出码 2）两种场景
- [x] 3.4 新增 `src/transfertools/__main__.py` 支持 `python -m transfertools`；验证 `uv run python -m transfertools --help` 可用

## 4. 递归目录复制（可选）

- [x] 4.1 实现 `-r/--recursive`：遍历源目录树，按相对路径在目标目录重建结构，文件逐一走第 2 节的复制+校验流程；验证集成测试：含子目录与多文件的目录树复制后逐一比对内容一致
- [x] 4.2 未启用 `-r` 时源为目录的报错路径；验证集成测试：报错信息提示需要递归选项且退出码非零

## 5. 收尾验证

- [x] 5.1 在 README.md 补充用法、退出码与「不规避所有行为审计」的说明；验证文档与 `--help` 一致
- [x] 5.2 全量验证：`uv run pytest` 全绿；`uv run safecopy` 对真实目录做一次单文件与多文件复制并人工核对 SHA-256（`sha256sum`）一致
