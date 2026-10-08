"""产品识别与图像预处理测试。"""
from __future__ import annotations

import io

from PIL import Image


def _make_image_bytes() -> bytes:
    img = Image.new("RGB", (500, 500), (210, 190, 170))
    for x in range(0, 500, 4):
        for y in range(0, 500, 4):
            img.putpixel((x, y), (120 + x % 100, 90 + y % 90, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_analyze_product(client):
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("test.png", _make_image_bytes(), "image/png")},
        data={"hint": "wireless earbuds"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["product"]["name"]
    assert isinstance(data["product"]["selling_points"], list)
    assert "quality" in data


def test_get_product(client):
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("test.png", _make_image_bytes(), "image/png")},
    )
    product_id = resp.json()["data"]["product"]["id"]
    resp2 = client.get(f"/api/v1/products/{product_id}")
    assert resp2.status_code == 200
    assert resp2.json()["data"]["id"] == product_id


def test_reject_invalid_file(client):
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("bad.txt", b"not an image", "text/plain")},
    )
    assert resp.status_code == 400


def test_update_merges_extra(client):
    """PUT 编辑应为增量合并：只覆盖本次提供的键，不丢其它已有字段。"""
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("test.png", _make_image_bytes(), "image/png")},
    )
    product_id = resp.json()["data"]["product"]["id"]

    # 第一次：设价格
    r1 = client.put(
        f"/api/v1/products/{product_id}",
        json={"price": "29.90", "extra": {"color_note": "red"}},
    )
    assert r1.status_code == 200
    assert r1.json()["data"]["extra"]["price"] == "29.90"

    # 第二次：只设促销（增量），不应清掉已有 price/color_note
    r2 = client.put(
        f"/api/v1/products/{product_id}",
        json={"promotion": "sale"},
    )
    assert r2.status_code == 200
    extra = r2.json()["data"]["extra"]
    assert extra["price"] == "29.90"
    assert extra["promotion"] == "sale"
    assert extra["color_note"] == "red"


def test_update_merge_conflict_retries(client, monkeypatch):
    """乐观锁冲突应重试合并，而不是直接覆盖丢失更新（用 stub 制造一次 rowcount=0）。

    直接命中第二次（重试）路径：把条件 UPDATE 首次执行包一层，返回 rowcount=0，
    然后再放行，最终结果仍应是完整合并。
    """
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("test.png", _make_image_bytes(), "image/png")},
    )
    product_id = resp.json()["data"]["product"]["id"]

    from sqlalchemy.sql.dml import Update

    from app.models import Product

    calls = {"n": 0}
    orig_execute = None
    import app.api.v1.products as mod

    # 对 db.execute 打桩：第一次 UPDATE 请求时强制 rowcount=0 模拟冲突
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        real = db.execute
        outer = {"once": True}

        def fake_execute(stmt, *a, **kw):
            nonlocal outer
            if outer["once"] and isinstance(stmt, Update):
                outer["once"] = False
                return _fake_result(0)
            return real(stmt, *a, **kw)

        monkeypatch.setattr(db, "execute", fake_execute)
        # 用同一 session 调用 update endpoint
        from app.api.v1.products import update as update_endpoint

        resp2 = update_endpoint(
            product_id,
            type("E", (), {"price": "99.00", "promotion": None,
                           "brand_name": None, "extra": {}})(),
            db,
        )
        assert resp2["data"]["extra"]["price"] == "99.00"
    finally:
        db.close()


def _fake_result(rowcount: int):
    class _R:
        def __init__(self, rc):
            self.rowcount = rc

    return _R(rowcount)
