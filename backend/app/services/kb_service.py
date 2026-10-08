"""知识库服务：文档入库、删除、列表、混合检索。"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, NotFoundError
from app.models import KbChunk, KbDocument
from app.rag.bm25 import BM25Retriever
from app.rag.embedding import get_embedding
from app.rag.retriever import hybrid_search, sanitize_metadata
from app.rag.splitter import parse_document, split_text
from app.rag.vector_store import get_vector_store
from app.schemas.kb import RagSource, SearchResult

# ---------- 检索缓存 ----------
# 每次检索都会全量读库并重建 BM25 索引，属 O(N) 开销。这里按
# (过滤条件, 索引版本) 缓存语料与 BM25 实例，知识库变更时整体失效。
#
# 缓存的 key 含用户传入的 country/platform/category，属**外部可控**输入：
# 不加限制时，构造大量不同过滤组合即可让缓存无上限增长（每个 value 还是整个语料
# 列表 + BM25 索引，内存放大数十倍）。因此设 LRU 上界，超限时淘汰最久未用的条目。
_CACHE_MAX_ENTRIES = 64
# 单个缓存条目：rows（语料）+ bm25（索引）。两个数据永远同生共死，放进同一条目，
# 避免「两个 OrderedDict 需同步操作才能保证一致」的并发复杂度。
_CacheEntry = tuple[list[tuple[str, str, dict[str, Any]]], "BM25Retriever"]
_cache: "OrderedDict[tuple[Any, ...], _CacheEntry]" = OrderedDict()
_index_version = 0
# 保护 _cache / _index_version 结构（读、写、move_to_end、popitem 都在锁内做）
_cache_lock = threading.Lock()


def _invalidate_search_cache() -> None:
    """知识库变更（入库/删除/重建）后使检索缓存失效。"""
    global _index_version
    with _cache_lock:
        _index_version += 1
        _cache.clear()


def _load_entry(
    db: Session,
    country: str | None,
    platform: str | None,
    category: str | None,
) -> _CacheEntry:
    """全库扫描 + BM25 构建（耗时较长，**在锁外**执行），返回 (rows, bm25)。"""
    rows = _load_chunk_rows(db, country, platform, category)
    bm25 = BM25Retriever(rows)
    return rows, bm25


def _get_cached_entry(
    db: Session,
    key: tuple[Any, ...],
    country: str | None,
    platform: str | None,
    category: str | None,
) -> _CacheEntry:
    """线程安全地取缓存条目；未命中则在锁外构建。

    设计要点：
    - 快速命中路径不加锁取 .get（读快），但**命中后的 LRU 更新时间**必须加锁，
      与淘汰逻辑的 _cache.popitem 互斥，避免 OrderedDict 在并发变异中结构损坏。
    - 未命中时在**锁外**执行 _load_entry（全库扫描 + BM25 构建），仅把结果
      回填进缓存时短暂加锁，避免构建期间阻塞所有并发检索线程。
    - 多个线程并发 miss 同一 key 时可能重复构建（幂等、结果一致），无害。
    """
    entry = _cache.get(key)
    if entry is not None:
        # 快速命中：仅刷新 LRU 新鲜度（锁内做，与淘汰互斥）
        with _cache_lock:
            _cache.move_to_end(key)
        return entry

    # 未命中：锁外构建，省去把全局锁握在手里的昂贵期间
    built = _load_entry(db, country, platform, category)
    with _cache_lock:
        existing = _cache.get(key)
        if existing is None:
            _cache[key] = built
            while len(_cache) > _CACHE_MAX_ENTRIES:
                _cache.popitem(last=False)
        else:
            built = existing
        _cache.move_to_end(key)
    return built


def ingest_document(
    db: Session,
    data: bytes,
    filename: str,
    title: str | None,
    country: str | None,
    platform: str | None,
    category: str | None,
) -> KbDocument:
    """解析 → 切分 → 向量化 → 入库。"""
    try:
        text = parse_document(data, filename)
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(f"文档解析失败: {exc}") from exc
    if not text.strip():
        raise BadRequestError("文档内容为空")

    chunks = split_text(text)
    if not chunks:
        raise BadRequestError("文档切分后无有效内容")

    doc = KbDocument(
        title=title or filename,
        source_type=filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt",
        country=country,
        platform=platform,
        category=category,
        status="processing",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    try:
        embedding = get_embedding()
        vectors = embedding.embed_documents(chunks)
        ids = [f"doc{doc.id}_{i}" for i in range(len(chunks))]
        metadatas = [
            sanitize_metadata(
                {
                    "document_id": doc.id,
                    "title": doc.title,
                    "country": country,
                    "platform": platform,
                    "category": category,
                }
            )
            for _ in chunks
        ]
        get_vector_store().add(ids, chunks, vectors, metadatas)

        for i, chunk in enumerate(chunks):
            db.add(
                KbChunk(
                    document_id=doc.id,
                    content=chunk,
                    embedding_id=ids[i],
                    meta=metadatas[i],
                    order_index=i,
                )
            )
        doc.status = "done"
        doc.chunk_count = len(chunks)
        db.commit()
        db.refresh(doc)
        _invalidate_search_cache()
        return doc
    except Exception as exc:  # noqa: BLE001
        doc.status = "failed"
        db.commit()
        raise BadRequestError(f"入库失败: {exc}") from exc


def delete_document(db: Session, document_id: int) -> None:
    doc = db.get(KbDocument, document_id)
    if doc is None:
        raise NotFoundError(f"文档 {document_id} 不存在")
    ids = [c.embedding_id for c in doc.chunks if c.embedding_id]
    if ids:
        get_vector_store().delete(ids)
    db.delete(doc)
    db.commit()
    _invalidate_search_cache()


def list_documents(db: Session) -> list[KbDocument]:
    return list(db.execute(select(KbDocument).order_by(KbDocument.id.desc())).scalars())


def _build_where(
    country: str | None, platform: str | None, category: str | None
) -> dict[str, Any] | None:
    where: dict[str, Any] = {}
    if country:
        where["country"] = country
    if platform:
        where["platform"] = platform
    if category:
        where["category"] = category
    return where or None


def _load_chunk_rows(
    db: Session,
    country: str | None,
    platform: str | None,
    category: str | None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """加载满足元数据过滤的候选块（供 BM25 使用）。"""
    stmt = select(KbChunk)
    if country or platform or category:
        stmt = stmt.join(KbDocument, KbChunk.document_id == KbDocument.id)
        if country:
            stmt = stmt.where(KbDocument.country == country)
        if platform:
            stmt = stmt.where(KbDocument.platform == platform)
        if category:
            stmt = stmt.where(KbDocument.category == category)
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for chunk in db.execute(stmt).scalars():
        meta = dict(chunk.meta or {})
        meta.setdefault("document_id", chunk.document_id)
        cid = chunk.embedding_id or f"doc{chunk.document_id}_{chunk.order_index}"
        rows.append((cid, chunk.content, meta))
    return rows


def search(
    db: Session,
    query: str,
    top_k: int = 5,
    country: str | None = None,
    platform: str | None = None,
    category: str | None = None,
) -> SearchResult:
    """混合检索并返回带引用来源的结果。"""
    where = _build_where(country, platform, category)
    key = (country, platform, category, _index_version)

    # 命中则不取锁（读快路径）；未命中才 double-check 后在锁外做耗时的
    # 全库扫描 + BM25 构建，避免构建期间阻塞所有其它并发检索线程。
    #
    # 并发下同 key 可能被多个线程各自构建一次（幂等、无害），但我们只允许
    # 在**锁外**构建，锁只负责「查缓存 + 写缓存 + 维护 LRU 顺序」，把耗时的
    # BM25 构建排除在全球锁之外（这才是真正的缓解点）。
    rows, bm25 = _get_cached_entry(db, key, country, platform, category)

    # 向量库初始化失败（依赖缺失 / 路径不可写等）不应中断检索，降级为关键词检索
    try:
        vector_store = get_vector_store()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"向量库不可用，检索降级为关键词模式: {exc}")
        vector_store = None

    hits = hybrid_search(
        query,
        vector_store,
        get_embedding(),
        rows,
        top_k=top_k,
        where=where,
        bm25=bm25,
    )
    sources = [
        RagSource(
            document_id=int(hit.metadata.get("document_id", 0)),
            title=hit.metadata.get("title", ""),
            content=hit.text,
            score=round(hit.score, 4),
            metadata=hit.metadata,
        )
        for hit in hits
    ]
    return SearchResult(sources=sources)


# 内置示例知识库映射：文件名 -> 元数据
_SEED_DOCS = [
    ("compliance_us.txt", "美国市场广告合规规则", "US", None, "compliance"),
    ("culture_us.txt", "美国市场文化与消费习惯", "US", None, "culture"),
    ("culture_jp.txt", "日本市场文化与消费习惯", "JP", None, "culture"),
    ("copy_examples.txt", "消费电子品类优秀文案范例", None, None, "copy"),
]


def seed_knowledge_base(db: Session, seed_dir: str | None = None) -> int:
    """知识库为空时，自动灌入内置示例文档。返回灌入数量。"""
    from pathlib import Path

    existing = db.execute(select(KbDocument.id).limit(1)).first()
    if existing is not None:
        return 0

    base = Path(seed_dir) if seed_dir else Path(__file__).resolve().parents[3] / "data" / "knowledge_seed"
    if not base.exists():
        return 0

    count = 0
    for filename, title, country, platform, category in _SEED_DOCS:
        path = base / filename
        if not path.exists():
            continue
        try:
            ingest_document(
                db,
                path.read_bytes(),
                filename,
                title,
                country,
                platform,
                category,
            )
            count += 1
        except Exception:  # noqa: BLE001  单个文档失败不阻断启动
            continue
    return count


def rebuild_index(db: Session) -> int:
    """清空向量库并从数据库块重建索引（用于索引丢失后的恢复）。"""
    chunks = list(db.execute(select(KbChunk)).scalars())
    store = get_vector_store()
    store.clear()
    if not chunks:
        _invalidate_search_cache()
        return 0
    ids = [
        c.embedding_id or f"doc{c.document_id}_{c.order_index}" for c in chunks
    ]
    texts = [c.content for c in chunks]
    metadatas = [dict(c.meta or {}) for c in chunks]
    vectors = get_embedding().embed_documents(texts)
    store.add(ids, texts, vectors, metadatas)
    _invalidate_search_cache()
    return len(ids)
