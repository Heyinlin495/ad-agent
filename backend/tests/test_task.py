"""广告生成全流程（Mock 模式）测试。"""
from __future__ import annotations

import io
import time

from PIL import Image


def _make_image_bytes() -> bytes:
    img = Image.new("RGB", (500, 500), (210, 190, 170))
    for x in range(0, 500, 4):
        for y in range(0, 500, 4):
            img.putpixel((x, y), (120 + x % 100, 90 + y % 90, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _analyze(client) -> int:
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("test.png", _make_image_bytes(), "image/png")},
    )
    return resp.json()["data"]["product"]["id"]


def test_generate_full_flow(client):
    product_id = _analyze(client)
    resp = client.post(
        "/api/v1/ads/generate",
        json={
            "product_id": product_id,
            "country": "US",
            "language": "en",
            "platform": "Amazon",
            "style": "promo",
            "size_preset": "1:1",
            "num_versions": 3,
        },
    )
    assert resp.status_code == 200
    task_id = resp.json()["data"]["task_id"]

    status = None
    for _ in range(100):
        st = client.get(f"/api/v1/ads/{task_id}").json()["data"]
        if st["status"] in {"success", "failed"}:
            status = st
            break
        time.sleep(0.1)

    assert status is not None, "任务超时未完成"
    assert status["status"] == "success", status.get("error")
    assert len(status["copies"]) == 3
    assert len(status["images"]) >= 1
    assert status["images"][0]["image_url"].startswith("/files/")
    # 文案应携带 RAG 引用来源
    assert any(c.get("rag_sources") for c in status["copies"])


def test_history(client):
    resp = client.get("/api/v1/history", params={"page": 1, "page_size": 10})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["total"] >= 1
    assert len(body["items"]) >= 1
