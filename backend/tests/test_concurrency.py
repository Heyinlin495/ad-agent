"""并发正确性与健壮性回归测试（对应批次一修复）。

覆盖：
- P0-1 SQLite 启用 WAL / busy_timeout
- P0-2 任务线程池背压（队列满抛 QueueFullError）
- P0-3 进度回调 ContextVar 在线程间隔离
- P0-4 compliance_check 失败降级为放行
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from app.agents import nodes
from app.agents.tools import compliance_check
from app.schemas.ad import ComplianceReport


# ---------- P0-1 SQLite 并发配置 ----------


def test_sqlite_pragmas_enabled(client):
    from app.core.database import engine

    if not str(engine.url).startswith("sqlite"):
        pytest.skip("非 SQLite 环境")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert int(conn.execute(text("PRAGMA busy_timeout")).scalar()) >= 30000


def test_concurrent_writes_do_not_lock(client):
    """多线程并发写同一 SQLite 文件不应抛 database is locked（WAL 生效）。"""
    from app.core.database import SessionLocal
    from app.models import AdTask, Product

    # 先落一个真实产品，满足 ad_tasks.product_id 的外键约束
    db0 = SessionLocal()
    try:
        product = Product(name="Concurrency Probe", category="Test")
        db0.add(product)
        db0.commit()
        pid = product.id
    finally:
        db0.close()

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            db = SessionLocal()
            try:
                for i in range(10):
                    db.add(AdTask(
                        product_id=pid, country="US", language="en", platform="Amazon",
                        style="promo", size_preset="1:1", status="pending",
                    ))
                    db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"并发写出现异常: {errors[:3]}"


# ---------- P0-2 线程池背压 ----------


def test_thread_pool_rejects_when_full(monkeypatch):
    from app.services import task_runner

    monkeypatch.setattr(task_runner, "_MAX_INFLIGHT", 2)
    monkeypatch.setattr(task_runner, "_inflight", 0)

    release = threading.Event()

    def blocking() -> None:
        release.wait(timeout=5)

    task_runner.submit(blocking)
    task_runner.submit(blocking)
    with pytest.raises(task_runner.QueueFullError):
        task_runner.submit(blocking)

    release.set()
    # 等待在途计数回落
    for _ in range(50):
        if task_runner.inflight_count() == 0:
            break
        threading.Event().wait(0.05)
    assert task_runner.inflight_count() == 0


# ---------- P0-3 进度回调线程隔离 ----------


def test_progress_sink_isolated_between_threads():
    """两个线程各自设置 sink，互不串台。"""
    seen: dict[str, list] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    def run(key: str) -> None:
        token = nodes.set_progress_sink(lambda p, n=None: seen[key].append(p))
        try:
            barrier.wait(timeout=5)  # 两个线程都装好 sink 后再各自 emit
            nodes.emit(11)
            nodes.emit(22)
        finally:
            nodes.reset_progress_sink(token)

    threads = [threading.Thread(target=run, args=(k,)) for k in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert seen["a"] == [11, 22]
    assert seen["b"] == [11, 22]


def test_progress_sink_reset_restores_default():
    token = nodes.set_progress_sink(lambda p, n=None: None)
    nodes.reset_progress_sink(token)
    assert nodes._progress_sink.get() is None


# ---------- P0-4 合规审查降级 ----------


def test_compliance_check_degrades_on_llm_error(monkeypatch):
    """LLM 抛异常时不得打死流水线，应返回放行 + 待复核标记。"""
    from app.agents import tools

    class BoomLLM:
        def chat_json(self, *a, **kw):
            raise ValueError("not json")

    monkeypatch.setattr(tools, "get_llm", lambda: BoomLLM())
    report = compliance_check("buy now", "US", "Amazon", ["不要虚假宣传"])
    assert isinstance(report, ComplianceReport)
    assert report.passed is True
    assert report.violations == []
    assert "复核" in (report.feedback or "")


def test_compliance_check_passes_through_valid_result(monkeypatch):
    from app.agents import tools

    class OkLLM:
        def chat_json(self, *a, **kw):
            return {"passed": False, "violations": [], "feedback": "含绝对化用语"}

    monkeypatch.setattr(tools, "get_llm", lambda: OkLLM())
    report = compliance_check("best ever", "US", "Amazon", [])
    assert report.passed is False
    assert report.feedback == "含绝对化用语"


# ---------- kb 检索缓存并发安全 ----------


def test_kb_search_cache_thread_safe(client):
    """多线程并发命中/未命中缓存不应抛 OrderedDict 并发变异错误。"""
    import threading

    from app.core.database import SessionLocal
    from app.services import kb_service

    errors: list[Exception] = []
    n_threads = 16
    per_thread = 30

    def worker(n: int) -> None:
        try:
            db = SessionLocal()
            try:
                for i in range(per_thread):
                    # 交替命中/未命中：制造 move_to_end + popitem + 构建并发
                    country = "US" if n % 2 == 0 else ("JP" if i % 2 else None)
                    kb_service.search(
                        db, "digital camera best seller", top_k=3,
                        country=country, platform=None, category="copy",
                    )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发检索出现异常: {errors[:3]}"
    # 缓存不应超出上界
    assert len(kb_service._cache) <= kb_service._CACHE_MAX_ENTRIES
