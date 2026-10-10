"""产品识别服务：预处理 → 多模态识别 → 持久化。"""
from __future__ import annotations

import io
import time

from PIL import Image
from loguru import logger
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError
from app.core.signing import sign_file_url
from app.image.preprocess import (
    ImageProcessError,
    apply_exif_orientation,
    compress_image,
    detect_quality,
    remove_background,
    validate_image,
)
from app.models import Product
from app.prompts import render_prompt
from app.schemas.product import AnalyzeResponse, ProductAnalysis, ProductOut, QualityReport
from app.services.llm import get_llm
from app.services.storage import storage


def analyze_product(
    db: Session, image_bytes: bytes, filename: str, hint: str | None = None
) -> AnalyzeResponse:
    """对上传的产品图执行完整分析，返回识别结果 + 质检报告。"""
    t0 = time.monotonic()
    try:
        img, mime = validate_image(image_bytes, filename)
    except ImageProcessError as exc:
        raise BadRequestError(str(exc)) from exc
    t_decode = time.monotonic()

    # 预处理：EXIF 纠正 → 压缩 → 质量检测
    img = apply_exif_orientation(img)
    compressed = compress_image(img)
    quality: QualityReport = detect_quality(compressed)
    t_pre = time.monotonic()

    # 去背景得到透明主体图
    subject = remove_background(compressed)
    t_rembg = time.monotonic()

    # 保存原图与主体图
    original_url = storage.save_bytes(
        _to_bytes(compressed.convert("RGB"), "JPEG"), filename
    )
    subject_url = storage.save_bytes(_to_bytes(subject, "PNG"), "subject.png")
    t_store = time.monotonic()

    # 多模态识别
    llm = get_llm()
    prompt = render_prompt("product_analysis", hint=hint or "无")
    result = llm.vision_json(
        image_bytes=_to_bytes(compressed.convert("RGB"), "JPEG"),
        image_mime="image/jpeg",
        prompt=prompt,
        purpose="product_analysis",
    )
    analysis = ProductAnalysis.model_validate(result)
    t_llm = time.monotonic()

    # 持久化
    product = Product(
        name=analysis.name,
        category=analysis.category,
        material=analysis.material,
        color=analysis.color,
        shape=analysis.shape,
        scenes=analysis.scenes,
        selling_points=analysis.selling_points,
        target_audience=analysis.target_audience,
        brand_suspected=analysis.brand_suspected,
        risk_flags=[f.model_dump() for f in analysis.risk_flags],
        subject_image_url=subject_url,
        original_image_url=original_url,
        extra={"hint": hint or ""},
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    t_end = time.monotonic()

    # 分段耗时：定位识别瓶颈的唯一依据（否则只能凭猜）
    logger.info(
        "[analyze] 耗时明细 | 解码 {:.2f}s | 预处理 {:.2f}s | 抠图 {:.2f}s | 存储 {:.2f}s | 视觉识别 {:.2f}s | 落库 {:.2f}s | 总计 {:.2f}s",
        t_decode - t0,
        t_pre - t_decode,
        t_rembg - t_pre,
        t_store - t_rembg,
        t_llm - t_store,
        t_end - t_llm,
        t_end - t0,
    )

    return AnalyzeResponse(
        product=ProductOut(
            id=product.id,
            subject_image_url=sign_file_url(product.subject_image_url),
            original_image_url=sign_file_url(product.original_image_url),
            extra=product.extra,
            **analysis.model_dump(),
        ),
        quality=quality,
    )


def get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"产品 {product_id} 不存在")
    return product


def _to_bytes(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    save_fmt = "JPEG" if fmt == "JPEG" else "PNG"
    img.save(buf, format=save_fmt)
    return buf.getvalue()
