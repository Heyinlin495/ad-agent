"""运行指标端点测试（JSON 快照 + Prometheus 文本 + 鉴权）。"""
from __future__ import annotations

from app.core import metrics


def test_metrics_json_envelope(client):
    """/metrics 返回标准信封，task/llm 键存在。"""
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert "uptime_seconds" in data
    assert "llm" in data and "tasks" in data and "counters" in data


def test_metrics_prometheus_format(client):
    """/metrics/prometheus 返回 Prometheus 文本格式与正确 content-type。"""
    r = client.get("/api/v1/metrics/prometheus")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
    text = r.text
    # 必须含 TYPE 声明与至少一个量，且以换行结尾
    assert "# TYPE ad_agent_tasks_total counter" in text
    assert "ad_agent_uptime_seconds" in text
    assert text.endswith("\n")
    # 标签值转义：任意含引号/反斜杠的 purpose 不破坏格式
    metrics.record_llm_call('we"ird\\purpose', 100, 2, 3, 0.1)
    r2 = client.get("/api/v1/metrics/prometheus")
    assert r2.status_code == 200
    assert 'purpose="we\\"ird\\\\purpose"' in r2.text


def test_metrics_prometheus_reflects_records(client):
    """prometheus 文本能反映新写入的任务终态计数。"""
    before = metrics.prometheus_text()
    assert "# TYPE ad_agent_tasks_total counter" in before
    metrics.record_task("success", 1200)
    metrics.record_llm_call("test_purpose", 42, 100, 200, 0.05)
    text = metrics.prometheus_text()
    assert 'ad_agent_tasks_total{status="success"} 1' in text
    assert 'ad_agent_llm_calls_total{purpose="test_purpose"} 1' in text


def test_metrics_requires_token_when_configured(client, monkeypatch):
    """配置 METRICS_TOKEN 后，未带 token 请求被拒绝，带对则放行。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "metrics_token", "secret123")
    try:
        # 无 token -> 403
        assert client.get("/api/v1/metrics").status_code == 403
        assert client.get("/api/v1/metrics/prometheus").status_code == 403
        # query 参数 token
        assert client.get("/api/v1/metrics", params={"token": "secret123"}).status_code == 200
        assert (
            client.get("/api/v1/metrics/prometheus", params={"token": "secret123"}).status_code
            == 200
        )
        # header token
        assert (
            client.get(
                "/api/v1/metrics", headers={"x-metrics-token": "secret123"}
            ).status_code
            == 200
        )
        # 错误 token 被拒
        assert (
            client.get("/api/v1/metrics", params={"token": "wrong"}).status_code == 403
        )
    finally:
        monkeypatch.setattr(settings, "metrics_token", "")