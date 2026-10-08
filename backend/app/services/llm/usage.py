"""LLM 用量日志写入（成本 / 耗时 / token）。"""
from __future__ import annotations

from app.core.database import SessionLocal
from app.models import UsageLog


def log_usage(
    task_id: int | None,
    provider: str,
    model: str,
    purpose: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    cost: float,
) -> None:
    """向 usage_logs 表写入一条记录。"""
    db = SessionLocal()
    try:
        db.add(
            UsageLog(
                task_id=task_id,
                provider=provider,
                model=model,
                purpose=purpose,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost=cost,
            )
        )
        db.commit()
    finally:
        db.close()
