"""混合检索：向量 + BM25 + RRF 融合 + Rerank。"""
from __future__ import annotations

from typing import Any

from loguru import logger

from app.rag.bm25 import BM25Retriever, rerank
from app.rag.embedding import EmbeddingProvider
from app.rag.vector_store import SearchHit, VectorStore

RRF_K = 60


def reciprocal_rank_fusion(
    lists: list[list[SearchHit]], k: int = RRF_K
) -> list[SearchHit]:
    """对多个排序结果做 RRF 融合。"""
    score_map: dict[str, float] = {}
    hit_map: dict[str, SearchHit] = {}
    for hits in lists:
        for rank, hit in enumerate(hits):
            score_map[hit.id] = score_map.get(hit.id, 0.0) + 1.0 / (k + rank + 1)
            hit_map[hit.id] = hit
    merged = [
        SearchHit(cid, hit_map[cid].text, score, hit_map[cid].metadata)
        for cid, score in sorted(score_map.items(), key=lambda x: x[1], reverse=True)
    ]
    return merged


def hybrid_search(
    query: str,
    vector_store: VectorStore | None,
    embedding: EmbeddingProvider | None,
    chunk_rows: list[tuple[str, str, dict[str, Any]]],
    top_k: int = 5,
    where: dict[str, Any] | None = None,
    bm25: BM25Retriever | None = None,
) -> list[SearchHit]:
    """执行混合检索并返回融合后命中。

    chunk_rows: [(id, text, metadata), ...] —— 已按元数据过滤的候选块。
    bm25: 可复用的 BM25 检索器（避免每次请求重建索引）；不传则就地构建。

    向量检索是**增强而非硬依赖**：Embedding 服务不可用（额度耗尽、账号欠费、
    网络异常、向量库缺失）时降级为纯 BM25 关键词检索，避免知识库侧故障
    直接把整条生成流水线打挂。
    """
    if not query.strip():
        return []

    # 1. 向量检索（候选扩到 2 倍）
    vector_hits: list[SearchHit] = []
    if vector_store is not None and embedding is not None:
        try:
            query_emb = embedding.embed_query(query)
            vector_hits = vector_store.query(query_emb, top_k * 2, where)
        except Exception as exc:  # noqa: BLE001  降级为关键词检索
            logger.warning(f"向量检索不可用，本次降级为纯关键词（BM25）检索: {exc}")

    # 2. BM25 关键词检索（复用外部缓存实例，未提供时就地构建）
    if bm25 is None:
        bm25 = BM25Retriever(chunk_rows)
    bm25_hits = bm25.search(query, top_k)

    # 3. RRF 融合
    merged = reciprocal_rank_fusion([vector_hits, bm25_hits])

    # 4. Rerank（可选）
    return rerank(query, merged)[:top_k]


def sanitize_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    """向量库元数据需为 str/int/float/bool，None 转空串。"""
    return {k: (v if v is not None else "") for k, v in meta.items()}
