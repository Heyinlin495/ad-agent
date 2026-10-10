"""产品识别与编辑接口。"""
import logging

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.rate_limit import heavy_rate_limit
from app.core.signing import sign_file_url
from app.core.uploads import read_upload_limited
from app.models import Product
from app.schemas.common import ok
from app.schemas.product import ProductEdit, ProductOut
from app.services import product_service
from app.services.product_service import get_product

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["products"])

# 乐观锁冲突重试上限
_MERGE_RETRIES = 3


def _merge_extra(old: dict | None, edit: ProductEdit) -> dict:
    """把编辑请求的字段合并进 extra（增量：只覆盖本次提供的键）。"""
    extra = dict(old or {})
    if edit.price is not None:
        extra["price"] = edit.price
    if edit.promotion is not None:
        extra["promotion"] = edit.promotion
    if edit.brand_name is not None:
        extra["brand_name"] = edit.brand_name
    extra.update(edit.extra or {})
    return extra


@router.post("/analyze", dependencies=[Depends(heavy_rate_limit)])
async def analyze(
    file: UploadFile = File(...),
    hint: str = Form(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """上传产品图，返回识别结果 + 质检报告。"""
    image_bytes = await read_upload_limited(file)
    # analyze_product 是纯同步的重活（抠图 / 视觉模型 / 对象存储，可达数十秒）。
    # 若直接在 async 端点里调用，会阻塞 uvicorn 事件循环——单 worker 下相当于
    # 冻结整个服务：识别期间 /health、/settings 全部无响应，前端随即显示
    # “后端未连接”。丢到线程池执行，事件循环得以继续处理其它请求。
    result = await run_in_threadpool(
        product_service.analyze_product,
        db,
        image_bytes,
        file.filename or "image.jpg",
        hint or None,
    )
    return ok(result.model_dump())


@router.get("/{product_id}")
def get(product_id: int, db: Session = Depends(get_db)) -> dict:
    product = get_product(db, product_id)
    return ok(
        ProductOut(
            id=product.id,
            name=product.name,
            category=product.category,
            material=product.material,
            color=product.color,
            shape=product.shape,
            scenes=product.scenes,
            selling_points=product.selling_points,
            target_audience=product.target_audience,
            brand_suspected=product.brand_suspected,
            risk_flags=product.risk_flags,
            subject_image_url=sign_file_url(product.subject_image_url),
            original_image_url=sign_file_url(product.original_image_url),
            extra=product.extra,
        ).model_dump()
    )


@router.put("/{product_id}")
def update(product_id: int, edit: ProductEdit, db: Session = Depends(get_db)) -> dict:
    """用户编辑补充：价格 / 促销 / 品牌名 / 自定义字段。

    extra 更新采用「乐观锁 + 条件 UPDATE」：先读当前值合并出目标字典，再用
    ``UPDATE ... WHERE id=:id AND extra=:old`` 原子写，rowcount=0 说明期间被
    其它请求修改过，则重新读、重新合并重试。避免并发编辑互相覆盖丢失更新。
    """
    product = get_product(db, product_id)

    for _ in range(_MERGE_RETRIES):
        extra = _merge_extra(product.extra, edit)
        old_extra = product.extra  # 合并前的原始快照（乐观锁依据）
        result = db.execute(
            sa_update(Product)
            .where(Product.id == product_id, Product.extra == old_extra)
            .values(extra=extra)
        )
        if result.rowcount:
            db.commit()
            # 返回最新合并结果（与库中一致）
            return ok({"id": product_id, "extra": extra})
        # 并发冲突：回滚本次尝试，重新读最新 extra 再合并
        db.rollback()
        product = get_product(db, product_id)

    # 多次冲突仍失败：抛冲突提示，不静默丢更新
    from fastapi import HTTPException

    raise HTTPException(status_code=409, detail="产品信息被并发修改，请稍后重试")
