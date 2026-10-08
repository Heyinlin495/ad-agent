"""文档解析与文本切分。"""
from __future__ import annotations

import io
from pathlib import Path


def parse_document(data: bytes, filename: str) -> str:
    """按扩展名解析文档为纯文本。"""
    ext = Path(filename).suffix.lower()
    if ext in {".txt", ".md", ".markdown"}:
        return data.decode("utf-8", errors="ignore")
    if ext == ".pdf":
        return _parse_pdf(data)
    if ext == ".docx":
        return _parse_docx(data)
    if ext == ".csv":
        return data.decode("utf-8", errors="ignore")
    # 默认按文本尝试
    return data.decode("utf-8", errors="ignore")


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _parse_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs)


def split_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> list[str]:
    """切分为块，优先使用 langchain 的 RecursiveCharacterTextSplitter。"""
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
        )
        return [c.strip() for c in splitter.split_text(text) if c.strip()]
    except Exception:  # noqa: BLE001  降级为朴素分块
        return _naive_split(text, chunk_size, chunk_overlap)


def _naive_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = end - overlap
    return [c for c in chunks if c]
