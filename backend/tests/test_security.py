"""安全与状态机回归测试（对应批次二修复）。

覆盖：
- P0-5 图片解压炸弹防护（像素上限）
- P0-6 任务状态机原子迁移（取消不被复活 / 不被成功覆盖）
- P1-2 /files 中间件路径规范化
"""
from __future__ import annotations

import io

import pytest
from PIL import Image


# ---------- P0-5 图片像素上限 ----------


def _png_of_size(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_image_pixel_limit_rejects_huge(monkeypatch):
    """超过像素上限的图片必须被拒（防解压炸弹）。"""
    from app.image.preprocess import ImageProcessError, validate_image

    # 造一张像素超限但文件很小的图：9000x9000 = 8100 万像素
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 40_000_000)
    data = _png_of_size(9000, 9000)
    with pytest.raises(ImageProcessError) as ei:
        validate_image(data, "huge.png")
    assert "像素" in str(ei.value)


def test_image_pixel_limit_rejects_bomb_error(monkeypatch):
    """PIL 自身抛出的 DecompressionBombError 也要被转成业务异常。"""
    from app.image.preprocess import ImageProcessError, validate_image

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100_000)
    data = _png_of_size(2000, 2000)  # 远高于上限，触发 PIL 的 bomb 保护
    with pytest.raises(ImageProcessError):
        validate_image(data, "bomb.png")


def test_image_pixel_limit_allows_normal():
    from app.image.preprocess import validate_image

    data = _png_of_size(800, 600)
    img, mime = validate_image(data, "ok.png")
    assert img.size == (800, 600)
    assert mime == "image/png"


# ---------- P0-6 任务状态机 ----------


def _make_task(client) -> int:
    """建一个 pending 任务（不启动执行，便于测状态迁移）。"""
    from app.core.database import SessionLocal
    from app.models import AdTask, Product

    db = SessionLocal()
    try:
        p = Product(name="State Probe", category="Test")
        db.add(p)
        db.commit()
        t = AdTask(
            product_id=p.id, country="US", language="en", platform="Amazon",
            style="promo", size_preset="1:1", status="pending",
        )
        db.add(t)
        db.commit()
        return t.id
    finally:
        db.close()


def test_cancel_then_regenerate_is_rejected(client):
    """取消后的任务不允许被重生成复活（原子 claim 生效）。"""
    from app.core.database import SessionLocal
    from app.core.exceptions import BadRequestError
    from app.models import AdTask
    from app.schemas.ad import RegenerateRequest
    from app.services import task_service

    tid = _make_task(client)
    db = SessionLocal()
    try:
        task_service.cancel_task(db, tid)
        assert db.get(AdTask, tid).status == "cancelled"
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        with pytest.raises(BadRequestError):
            task_service.schedule_regenerate(db2, tid, RegenerateRequest(mode="image"))
        assert db2.get(AdTask, tid).status == "cancelled"
    finally:
        db2.close()


def test_finish_task_does_not_override_cancelled(client):
    """任务被取消后，执行线程的终态写入不得覆盖为 success。"""
    from app.core.database import SessionLocal
    from app.models import AdTask
    from app.services import task_service

    tid = _make_task(client)
    db = SessionLocal()
    try:
        task_service.cancel_task(db, tid)
    finally:
        db.close()

    ok = task_service._finish_task(tid, "success", progress=100)
    assert ok is False
    db = SessionLocal()
    try:
        assert db.get(AdTask, tid).status == "cancelled"
    finally:
        db.close()


def test_cancel_does_not_override_success(client):
    """已成功的任务再取消，不应被改成 cancelled。"""
    from app.core.database import SessionLocal
    from app.models import AdTask
    from app.services import task_service

    tid = _make_task(client)
    assert task_service._finish_task(tid, "success", progress=100) is True
    db = SessionLocal()
    try:
        task_service.cancel_task(db, tid)
        assert db.get(AdTask, tid).status == "success"
    finally:
        db.close()


def test_regenerate_allowed_after_success(client):
    """成功后的任务允许重生成（正常业务路径不被原子 claim 误伤）。"""
    from app.core.database import SessionLocal
    from app.models import AdTask
    from app.schemas.ad import RegenerateRequest
    from app.services import task_service

    tid = _make_task(client)
    assert task_service._finish_task(tid, "success", progress=100) is True
    db = SessionLocal()
    try:
        task, _ = task_service.schedule_regenerate(db, tid, RegenerateRequest(mode="image"))
        assert task.status == "pending"
        assert db.get(AdTask, tid).status == "pending"
    finally:
        db.close()


# ---------- P1-2 /files 路径规范化 ----------


def test_files_path_normalization():
    """路径规范化：编码后的穿越必须被识别，正常路径保持原样。"""
    from app.main import normalize_request_path

    # 正常路径
    assert normalize_request_path("/files/abc.png") == ("/files/abc.png", False)
    # 百分号编码的 ../
    assert normalize_request_path("/files/%2e%2e/app/main.py")[1] is True
    # 直接 ../
    assert normalize_request_path("/files/../app/main.py")[1] is True
    # 路径中的 ./ 被折叠但不误判为穿越
    assert normalize_request_path("/files/./abc.png") == ("/files/abc.png", False)


def test_files_signature_uses_normalized_path(monkeypatch):
    """签名校验对象必须是规范化后的路径（否则校验与访问对象可能不一致）。"""
    import time

    from app.core import signing

    monkeypatch.setattr(signing.settings, "file_url_secret", "topsecret", raising=False)
    url = signing.sign_file_url("/files/abc.png", ttl=60)
    assert "sig=" in url
    # 用规范化路径 + 同一签名可通过校验
    from urllib.parse import parse_qs, urlsplit

    qs = parse_qs(urlsplit(url).query)
    assert signing.verify_file_request("/files/abc.png", qs["exp"][0], qs["sig"][0]) is True
    # 换一条路径则签名不匹配
    assert signing.verify_file_request("/files/other.png", qs["exp"][0], qs["sig"][0]) is False


def test_signature_expiry_rejected(monkeypatch):
    import time

    from app.core import signing

    monkeypatch.setattr(signing.settings, "file_url_secret", "topsecret", raising=False)
    expired = int(time.time()) - 10
    sig = signing._signature("/files/abc.png", expired)
    assert signing.verify_file_request("/files/abc.png", str(expired), sig) is False


# ---------- 错误编码区分（#12） ----------


def test_route_not_found_uses_distinct_code(client):
    """路由未命中的 404 应与业务 NotFoundError(40400) 分开编码（40410）。"""
    resp = client.get("/api/v1/no-such-endpoint")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40410


def test_business_not_found_uses_40400(client):
    """业务资源不存在仍用 40400，不与路由 40410 混淆。"""
    resp = client.get("/api/v1/products/999999")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40400
