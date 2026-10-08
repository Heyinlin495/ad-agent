"""知识库相关 Schema。"""
from typing import Any

from pydantic import BaseModel, Field


class DocumentMeta(BaseModel):
    country: str | None = None
    platform: str | None = None
    category: str | None = None
    title: str | None = None


class DocumentOut(BaseModel):
    id: int
    title: str
    source_type: str
    country: str | None = None
    platform: str | None = None
    category: str | None = None
    status: str
    chunk_count: int
    created_at: str | None = None


class RagSource(BaseModel):
    """检索引用来源。"""

    document_id: int
    title: str = ""
    content: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    country: str | None = None
    platform: str | None = None
    category: str | None = None


class SearchResult(BaseModel):
    sources: list[RagSource] = Field(default_factory=list)
