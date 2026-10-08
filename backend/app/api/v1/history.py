"""历史记录接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import NotFoundError
from app.models import AdTask
from app.schemas.common import ok

router = APIRouter(prefix="/history", tags=["history"])


@router.get("")
def list_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    total = db.execute(select(func.count()).select_from(AdTask)).scalar_one()
    tasks = list(
        db.execute(
            select(AdTask).order_by(AdTask.id.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars()
    )
    items = [
        {
            "id": t.id,
            "product_id": t.product_id,
            "country": t.country,
            "language": t.language,
            "platform": t.platform,
            "style": t.style,
            "status": t.status,
            "progress": t.progress,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in tasks
    ]
    return ok({"total": total, "page": page, "page_size": page_size, "items": items})


@router.get("/{task_id}")
def get_detail(task_id: int, db: Session = Depends(get_db)) -> dict:
    from app.services.task_service import get_status

    return ok(get_status(db, task_id))


@router.delete("/{task_id}")
def delete(task_id: int, db: Session = Depends(get_db)) -> dict:
    task = db.get(AdTask, task_id)
    if task is None:
        raise NotFoundError(f"任务 {task_id} 不存在")
    db.delete(task)
    db.commit()
    return ok({"deleted": task_id})
