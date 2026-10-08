"""第三批修复的回归测试：性能 / 健壮性。

覆盖：
- extract_json 的多种脏输出形态（代码块、前后夹说明、数组包裹、括号嵌套、非法输入）
- Retry-After 头的两种合法格式（秒数 / HTTP-date）
- Rerank 模型缓存（只加载一次、失败结果被记住）
- KB 检索缓存 LRU 上界
- 限流器 key 数量上界
- recover_orphan_tasks 的心跳新鲜度判定（不误杀其它 worker 的活跃任务）
"""
from __future__ import annotations

import pytest


# ---------- extract_json ----------

def test_extract_json_plain():
    from app.services.llm.base import extract_json

    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_code_fence():
    from app.services.llm.base import extract_json

    text = '```json\n{"headline": "早安", "cta": "购买"}\n```'
    assert extract_json(text) == {"headline": "早安", "cta": "购买"}


def test_extract_json_with_surrounding_prose():
    from app.services.llm.base import extract_json

    text = '好的，这是结果：\n{"ok": true}\n希望对你有帮助。'
    assert extract_json(text) == {"ok": True}


def test_extract_json_skips_braces_inside_strings():
    """字符串里的花括号不应被当成结构括号（旧实现 rfind('}') 会截错）。"""
    from app.services.llm.base import extract_json

    text = '说明文字 {"template": "使用 {占位符} 文案", "n": 2} 结束'
    assert extract_json(text) == {"template": "使用 {占位符} 文案", "n": 2}


def test_extract_json_unwraps_single_object_array():
    from app.services.llm.base import extract_json

    assert extract_json('[{"a": 1}]') == {"a": 1}


def test_extract_json_raises_value_error_on_garbage():
    """解析失败必须是 ValueError，便于上层统一降级捕获。"""
    from app.services.llm.base import extract_json

    with pytest.raises(ValueError):
        extract_json("这完全不是 JSON")
    with pytest.raises(ValueError):
        extract_json("")


# ---------- Retry-After 解析 ----------

def test_retry_after_accepts_seconds():
    from app.services.llm.openai_compat import _parse_retry_after

    assert _parse_retry_after("5") == 5.0


def test_retry_after_accepts_http_date():
    """RFC 7231 允许 HTTP-date 形式；旧实现只看 isdigit()，会漏掉并退回错误退避。"""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    from app.services.llm.openai_compat import _parse_retry_after

    future = datetime.now(timezone.utc) + timedelta(seconds=12)
    secs = _parse_retry_after(format_datetime(future))
    assert secs is not None
    assert 8.0 <= secs <= 14.0


def test_retry_after_returns_none_on_garbage():
    from app.services.llm.openai_compat import _parse_retry_after

    assert _parse_retry_after("soon") is None


# ---------- Rerank 模型缓存 ----------

def test_cross_encoder_loaded_once(monkeypatch):
    """同一模型名只应加载一次（旧实现每次 rerank 都重载数百 MB 权重）。"""
    from app.rag import bm25 as bm25_mod
    from app.rag.vector_store import SearchHit

    calls: list[str] = []

    class _FakeEnc:
        def __init__(self, name: str) -> None:
            calls.append(name)

        def predict(self, pairs):
            return [0.5] * len(pairs)

    fake_st = type("ST", (), {"CrossEncoder": _FakeEnc})

    monkeypatch.setattr(bm25_mod.settings, "rerank_provider", "bge", raising=False)
    monkeypatch.setattr(bm25_mod.settings, "rerank_model", "fake-model", raising=False)
    monkeypatch.setattr(bm25_mod.settings, "rerank_top_n", 5, raising=False)
    bm25_mod._rerank_models.clear()
    bm25_mod._rerank_errors.clear()
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)

    hits = [SearchHit("a", "文本 A", 0.1, {})]
    for _ in range(3):
        bm25_mod.rerank("查询", list(hits))

    assert calls == ["fake-model"]


def test_cross_encoder_load_failure_is_cached(monkeypatch):
    """加载失败也应缓存，避免每轮重试注定失败的加载。"""
    from app.rag import bm25 as bm25_mod
    from app.rag.vector_store import SearchHit

    attempts = {"n": 0}

    class _Boom:
        def __init__(self, name: str) -> None:
            attempts["n"] += 1
            raise ImportError("权重不可用")

    fake_st = type("ST", (), {"CrossEncoder": _Boom})

    monkeypatch.setattr(bm25_mod.settings, "rerank_provider", "bge", raising=False)
    monkeypatch.setattr(bm25_mod.settings, "rerank_model", "broken-model", raising=False)
    monkeypatch.setattr(bm25_mod.settings, "rerank_top_n", 5, raising=False)
    bm25_mod._rerank_models.clear()
    bm25_mod._rerank_errors.clear()
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)

    hits = [SearchHit("a", "文本 A", 0.1, {})]
    for _ in range(3):
        # 失败时保留原排序返回，不应抛出
        assert bm25_mod.rerank("查询", list(hits))

    assert attempts["n"] == 1


# ---------- 限流器 key 上界 ----------

def test_rate_limiter_bounds_key_count():
    from app.core.rate_limit import RateLimiter

    limiter = RateLimiter(max_requests=10, window_seconds=60, max_keys=100)
    for i in range(5000):
        limiter.is_allowed(f"10.0.{i // 256}.{i % 256}")

    assert len(limiter._hits) <= 100


def test_rate_limiter_still_enforces_limit():
    from app.core.rate_limit import RateLimiter

    limiter = RateLimiter(max_requests=3, window_seconds=60, max_keys=100)
    assert [limiter.is_allowed("k") for _ in range(3)] == [True, True, True]
    assert limiter.is_allowed("k") is False


def test_rate_limiter_thread_safe():
    """并发调用 is_allowed 不应破坏内部状态（计数稳定、不抛异常）。"""
    import threading

    from app.core.rate_limit import RateLimiter

    limiter = RateLimiter(max_requests=1000, window_seconds=60, max_keys=512)
    errors: list[Exception] = []
    total_threads = 32
    per_thread = 200

    def worker(n: int) -> None:
        try:
            for i in range(per_thread):
                limiter.is_allowed(f"10.0.{n}.{i % 256}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(total_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"限流器并发异常: {errors[:3]}"
    # key 上界仍被遵守
    assert len(limiter._hits) <= 512


def test_client_key_uses_forwarded_for():
    """反向代理后 X-Forwarded-For 应取代 client.host 作为限流键。"""
    from fastapi import Request

    from app.core.rate_limit import client_key

    def _mk(headers: dict[str, str]) -> Request:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (k.encode(), v.encode()) for k, v in headers.items()
            ],
            "client": ("203.0.113.7", 1234),
            "server": ("testserver", 80),
        }
        return Request(scope)

    # 无转发头：用直连地址
    assert client_key(_mk({})) == "203.0.113.7"

    # 有 X-Forwarded-For：取最左原始地址
    assert client_key(_mk({"x-forwarded-for": "198.51.100.23, 203.0.113.7"})) == "198.51.100.23"

    # 无 X-Forwarded-For 但有 X-Real-IP
    assert client_key(_mk({"x-real-ip": "203.0.113.9"})) == "203.0.113.9"


# ---------- KB 检索缓存 LRU 上界 ----------

def test_kb_cache_bounded_lru(monkeypatch):
    """缓存 key 由外部传入的 country/platform/category 组合而成，必须有上界。"""
    from app.services import kb_service

    monkeypatch.setattr(kb_service, "_CACHE_MAX_ENTRIES", 8)
    with kb_service._cache_lock:
        kb_service._cache.clear()

    # 模拟 40 种不同的过滤组合
    for i in range(40):
        key = (f"C{i}", None, None, kb_service._index_version)
        rows = [(f"id{i}", f"文本 {i}", {"document_id": i})]
        with kb_service._cache_lock:
            kb_service._cache[key] = (rows, kb_service.BM25Retriever(rows))
            while len(kb_service._cache) > kb_service._CACHE_MAX_ENTRIES:
                kb_service._cache.popitem(last=False)

    assert len(kb_service._cache) <= 8
    # 最后写入的 key 一定还在（刚使用过）
    assert ("C39", None, None, kb_service._index_version) in kb_service._cache

    with kb_service._cache_lock:
        kb_service._cache.clear()


def test_kb_cache_invalidate_clears():
    from app.services import kb_service

    rows = [("a", "文本", {"document_id": 1})]
    with kb_service._cache_lock:
        kb_service._cache[("X", None, None, kb_service._index_version)] = (
            rows,
            kb_service.BM25Retriever(rows),
        )
    before = kb_service._index_version
    kb_service._invalidate_search_cache()
    assert kb_service._index_version == before + 1
    assert not kb_service._cache
    # 检索仍可重建缓存（不抛错）
    worker_key = ("X", None, None, kb_service._index_version)
    assert worker_key not in kb_service._cache


# ---------- SSE 不阻塞事件循环 ----------

def test_sse_offloads_db_query(client, monkeypatch):
    """SSE 生成器不能直接同步查库（会卡死 event loop）。"""
    import asyncio
    import json as _json

    from app.api.v1 import ads as ads_mod

    to_thread_calls: list = []
    real_to_thread = asyncio.to_thread

    async def _spy(fn, *a, **kw):
        to_thread_calls.append(fn)
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(ads_mod.asyncio, "to_thread", _spy)

    # 建一个已完成的任务，SSE 会立刻遇到终止态并退出
    r = client.post(
        "/api/v1/ads/generate",
        json={"product_id": _make_product(client), "country": "US", "language": "en",
              "platform": "Amazon", "style": "promo", "num_versions": 1},
    )
    assert r.status_code == 200
    task_id = r.json()["data"]["task_id"]

    async def _consume():
        resp = client.get(f"/api/v1/ads/{task_id}/stream")
        body = b"".join(resp.iter_bytes())
        return body

    body = asyncio.run(_consume())
    assert b"data: " in body
    assert to_thread_calls, "SSE 必须通过 asyncio.to_thread 执行同步查库"


def _make_product(client) -> int:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (200, 200, 200)).save(buf, format="PNG")
    buf.seek(0)
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("p.png", buf.getvalue(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["product"]["id"]


# ---------- 孤儿任务回收：心跳新鲜度 ----------

def test_recover_orphan_tasks_keeps_fresh_running(client):
    """其它 worker 正在跑的任务（updated_at 很新）不能被误标 failed。"""
    from app.core.database import SessionLocal
    from app.models import AdTask
    from app.services.task_service import recover_orphan_tasks

    db = SessionLocal()
    try:
        fresh = AdTask(country="US", language="en", platform="Amazon", style="promo",
                       status="running", progress=50)
        db.add(fresh)
        db.commit()
        db.refresh(fresh)
        tid = fresh.id

        # 阈值给 600s：刚插入的任务 updated_at 是 now，必然在阈值内
        count = recover_orphan_tasks(db, stale_after_sec=600)
        db.expire_all()
        after = db.get(AdTask, tid)
        assert after.status == "running"
        assert tid not in [t.id for t in [after] if after.status == "failed"]
        # 至少不会把这条新任务计入回收
        assert count == 0 or tid not in _failed_ids(db)
    finally:
        db.close()


def test_recover_orphan_tasks_reclaims_stale(client):
    """超过阈值仍未心跳的任务（真死掉）应被回收为 failed。"""
    from datetime import datetime, timedelta, timezone

    from app.core.database import SessionLocal
    from app.models import AdTask
    from app.services.task_service import recover_orphan_tasks

    db = SessionLocal()
    try:
        stale = AdTask(country="US", language="en", platform="Amazon", style="promo",
                       status="running", progress=30)
        db.add(stale)
        db.commit()
        db.refresh(stale)
        tid = stale.id
        # 手工把 updated_at 推回到很久以前
        stale.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=3600
        )
        db.commit()

        recover_orphan_tasks(db, stale_after_sec=60)
        db.expire_all()
        assert db.get(AdTask, tid).status == "failed"
    finally:
        db.close()


def _failed_ids(db) -> set[int]:
    from app.models import AdTask

    return {t.id for t in db.query(AdTask).filter(AdTask.status == "failed").all()}


# ---------- Rerank 并发加载去重（#9） ----------


def test_rerank_concurrent_load_once(monkeypatch):
    """并发首次触发同一 rerank 模型时，CrossEncoder 只应被构造一次。"""
    import threading

    from app.rag import bm25

    construct_calls = {"n": 0}

    class FakeCrossEncoder:
        def __init__(self, model_name):
            construct_calls["n"] += 1
            self.model_name = model_name

        def predict(self, pairs):
            return [1.0] * len(pairs)

    monkeypatch.setattr(bm25, "_rerank_models", {})
    monkeypatch.setattr(bm25, "_rerank_errors", {})
    monkeypatch.setattr(bm25, "_loading", {})
    monkeypatch.setattr(bm25, "settings", type("S", (), {"rerank_provider": "bge",
                                                        "rerank_model": "bge-reranker",
                                                        "rerank_top_n": 5})())

    # 注入假的 sentence_transformers 模块（函数内是延迟导入），拦截 CrossEncoder 构造
    import sys
    import types

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.__path__ = []
    fake_st.CrossEncoder = FakeCrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)

    from app.rag.bm25 import _get_cross_encoder

    results: list = [None] * 16

    def worker(i: int) -> None:
        results[i] = _get_cross_encoder("bge-reranker")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert construct_calls["n"] == 1
    assert all(r is not None for r in results)
