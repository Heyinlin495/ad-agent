"""向量库（可配置：Chroma / Milvus，默认 Chroma + 内存兜底）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np
from loguru import logger

from app.core.config import settings


@dataclass
class SearchHit:
    """检索命中结果。"""

    id: str
    text: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(ABC):
    """向量库统一接口。"""

    @abstractmethod
    def add(
        self,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """批量写入。"""

    @abstractmethod
    def query(
        self, query_embedding: list[float], top_k: int, where: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        """向量检索。"""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        """按 id 删除。"""

    @abstractmethod
    def clear(self) -> None:
        """清空所有数据。"""

    @abstractmethod
    def count(self) -> int:
        """返回向量数量。"""


class InMemoryVectorStore(VectorStore):
    """纯内存向量库（无外部依赖，mock 兜底）。"""

    def __init__(self) -> None:
        self._texts: dict[str, str] = {}
        self._metas: dict[str, dict[str, Any]] = {}
        self._vecs: dict[str, np.ndarray] = {}

    def add(
        self,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        for i, cid in enumerate(ids):
            self._texts[cid] = texts[i]
            self._metas[cid] = metadatas[i]
            self._vecs[cid] = np.asarray(embeddings[i], dtype=np.float32)

    def query(
        self, query_embedding: list[float], top_k: int, where: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        q = np.asarray(query_embedding, dtype=np.float32)
        qn = np.linalg.norm(q)
        hits: list[SearchHit] = []
        for cid, vec in self._vecs.items():
            if not self._match_where(self._metas[cid], where):
                continue
            denom = float(np.linalg.norm(vec)) * qn
            score = float(np.dot(vec, q) / denom) if denom > 0 else 0.0
            hits.append(
                SearchHit(cid, self._texts[cid], score, self._metas[cid])
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def delete(self, ids: list[str]) -> None:
        for cid in ids:
            self._texts.pop(cid, None)
            self._metas.pop(cid, None)
            self._vecs.pop(cid, None)

    def clear(self) -> None:
        self._texts.clear()
        self._metas.clear()
        self._vecs.clear()

    def count(self) -> int:
        return len(self._vecs)

    @staticmethod
    def _match_where(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
        if not where:
            return True
        return all(meta.get(k) == v for k, v in where.items())


class ChromaVectorStore(VectorStore):
    """Chroma 持久化向量库。"""

    def __init__(self, collection_name: str = "kb_chunks") -> None:
        import chromadb  # 延迟导入
        from chromadb.config import Settings

        self.client = chromadb.PersistentClient(
            path=settings.chroma_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(collection_name)

    def add(
        self,
        ids: list[str],
        texts: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        self.collection.add(
            ids=ids,
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
        )

    def query(
        self, query_embedding: list[float], top_k: int, where: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        result = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        hits: list[SearchHit] = []
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        dists = result.get("distances", [[]])[0]
        for i, cid in enumerate(ids):
            # Chroma 默认 L2 距离，越小越相似，转为相似度分数
            score = 1.0 / (1.0 + float(dists[i]))
            hits.append(SearchHit(cid, docs[i], score, metas[i] or {}))
        return hits

    def delete(self, ids: list[str]) -> None:
        self.collection.delete(ids=ids)

    def clear(self) -> None:
        self.client.delete_collection(self.collection.name)
        self.collection = self.client.get_or_create_collection(self.collection.name)

    def count(self) -> int:
        return self.collection.count()


@lru_cache
def get_vector_store() -> VectorStore:
    """按配置返回向量库实例（单例缓存）。"""
    if settings.vector_store_type == "mock":
        return InMemoryVectorStore()
    if settings.vector_store_type == "chroma":
        try:
            return ChromaVectorStore()
        except Exception as exc:  # noqa: BLE001  chromadb 不可用时兜底内存
            logger.warning(f"Chroma 初始化失败，降级内存向量库: {exc}")
            return InMemoryVectorStore()
    if settings.vector_store_type == "milvus":
        try:
            from app.rag.milvus_store import MilvusVectorStore  # 延迟导入

            return MilvusVectorStore()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Milvus 初始化失败，降级内存向量库: {exc}")
            return InMemoryVectorStore()
    return InMemoryVectorStore()
