"""transfertools：分块写入式安全文件复制工具。

核心能力见 :mod:`transfertools.copier`（创建目标文件 → 分块写入 → fsync →
SHA-256 双端校验 → 失败清理），命令行入口见 :mod:`transfertools.cli`。
"""

from __future__ import annotations

from .copier import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNK_SIZE,
    MIB,
    MIN_CHUNK_SIZE,
    STAGE_DONE,
    STAGE_VERIFY,
    STAGE_WRITE,
    CopyCancelledError,
    CopyResult,
    DestinationError,
    InvalidChunkSizeError,
    SafeCopyError,
    SameFileError,
    SourceError,
    VerificationError,
    canonical_path,
    copy_file,
    is_same_file,
    is_within,
    iter_chunks,
    resolve_destination,
    sha256_file,
    validate_chunk_size,
    verify_digests,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "MIB",
    "DEFAULT_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "MAX_CHUNK_SIZE",
    "STAGE_WRITE",
    "STAGE_VERIFY",
    "STAGE_DONE",
    "SafeCopyError",
    "InvalidChunkSizeError",
    "SourceError",
    "DestinationError",
    "SameFileError",
    "VerificationError",
    "CopyCancelledError",
    "CopyResult",
    "validate_chunk_size",
    "iter_chunks",
    "sha256_file",
    "verify_digests",
    "resolve_destination",
    "canonical_path",
    "is_same_file",
    "is_within",
    "copy_file",
]
