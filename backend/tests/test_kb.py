"""知识库入库与检索测试。"""
from __future__ import annotations


def test_ingest_and_search(client):
    content = (
        "Wireless Bluetooth earbuds with active noise cancellation, "
        "30-hour battery life and IPX5 water resistance for sports."
    )
    resp = client.post(
        "/api/v1/kb/documents",
        files={"file": ("sample.txt", content.encode("utf-8"), "text/plain")},
        data={"title": "Earbuds Copy", "country": "US", "platform": "Amazon", "category": "copy"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "done"
    assert body["data"]["chunk_count"] >= 1

    resp2 = client.post("/api/v1/kb/search", json={"query": "noise cancelling earbuds", "top_k": 3})
    assert resp2.status_code == 200
    sources = resp2.json()["data"]["sources"]
    assert len(sources) >= 1
    assert sources[0]["content"]


def test_list_and_delete(client):
    resp = client.get("/api/v1/kb/documents")
    assert resp.status_code == 200
    docs = resp.json()["data"]
    assert isinstance(docs, list)
    if docs:
        doc_id = docs[0]["id"]
        resp2 = client.delete(f"/api/v1/kb/documents/{doc_id}")
        assert resp2.status_code == 200


def test_hybrid_search_degrades_to_bm25_when_embedding_fails():
    """Embedding 服务不可用时（如账号欠费），检索应降级为纯 BM25 而非抛异常。

    知识库是增强能力，其上游故障不能把整条广告生成流水线打挂。
    """
    from app.rag.retriever import hybrid_search

    rows = [
        ("c1", "active noise cancellation earbuds for sports", {"document_id": 1, "title": "A"}),
        ("c2", "waterproof bluetooth speaker with deep bass", {"document_id": 2, "title": "B"}),
    ]

    class BrokenEmbedding:
        def embed_query(self, text: str):  # noqa: ARG002
            raise RuntimeError("Arrearage: account is in overdue-payment status")

    class ExplodingStore:
        def query(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("embedding 已失败，不应再访问向量库")

    hits = hybrid_search(
        "earbuds noise cancellation",
        ExplodingStore(),
        BrokenEmbedding(),
        rows,
        top_k=3,
    )
    assert hits, "向量检索失败时应由 BM25 兜底返回结果"
    assert any("earbuds" in h.text for h in hits)


def test_hybrid_search_skips_vector_when_store_missing():
    """向量库不可用（None）时同样走关键词检索。"""
    from app.rag.retriever import hybrid_search

    rows = [("c1", "premium leather wallet for men", {"document_id": 1, "title": "W"})]
    hits = hybrid_search("leather wallet", None, None, rows, top_k=3)
    assert hits and any("wallet" in h.text for h in hits)
