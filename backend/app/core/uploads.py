"""上传文件读取工具。

分块读取上传文件并在超过大小上限时立即中断，避免一次性把超大文件读进内存。
"""
from __future__ import annotations

from fastapi import UploadFile

from app.core.config import settings
from app.core.exceptions import BadRequestError

_CHUNK_SIZE = 1024 * 1024  # 1MB


async def read_upload_limited(file: UploadFile, max_mb: int | None = None) -> bytes:
    """分块读取 UploadFile，超过大小上限抛 BadRequestError。

    - 优先用 UploadFile.size 快速拒绝（省去读取开销）；
    - 仍以实际读取字节数兜底，防止 size 缺失或被伪造。
    """
    limit_mb = max_mb if max_mb is not None else settings.max_upload_size_mb
    limit = limit_mb * 1024 * 1024

    size = getattr(file, "size", None)
    if size is not None and size > limit:
        raise BadRequestError(f"文件超过 {limit_mb}MB 限制")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise BadRequestError(f"文件超过 {limit_mb}MB 限制")
        chunks.append(chunk)
    return b"".join(chunks)
