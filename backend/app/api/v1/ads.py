"""广告生成任务接口（含 SSE 进度流）。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.exceptions import AppError
from app.core.rate_limit import heavy_rate_limit
from app.schemas.ad import GenerateRequest, RegenerateRequest
from app.schemas.common import ok
from app.services import task_runner, task_service

router = APIRouter(prefix="/ads", tags=["ads"])

# SSE 轮询间隔与连接寿命上限（秒）。
# 上限用于兜底回收"客户端半开连接"：浏览器/网关异常断开时服务端常无法立刻感知，
# 若不设上限，连接会随任务一起悬停并持续占用 worker。
SSE_POLL_INTERVAL = 0.5
SSE_MAX_DURATION = 1800.0


class TaskQueueFullError(AppError):
    """任务队列已满：在途任务数达到上限，拒绝新的提交。"""

    code = 42901
    http_status = 429
    message = "生成任务队列已满，请稍后重试"


def _submit_task(*args, **kwargs) -> None:
    """提交后台任务；队列满时转成统一的 429 响应。"""
    try:
        task_runner.submit(*args, **kwargs)
    except task_runner.QueueFullError as exc:
        raise TaskQueueFullError(message=str(exc)) from exc


@router.post("/generate", dependencies=[Depends(heavy_rate_limit)])
def generate(
    req: GenerateRequest,
    db: Session = Depends(get_db),
) -> dict:
    """创建异步生成任务，返回 task_id。"""
    task = task_service.create_task(db, req)
    _submit_task(task_service.run_task, task.id)
    return ok({"task_id": task.id})


@router.get("/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)) -> dict:
    return ok(task_service.get_status(db, task_id))


@router.get("/{task_id}/stream")
async def stream(task_id: int, request: Request) -> StreamingResponse:
    """SSE 进度流：轮询任务状态直至结束。

    两个关键点：
    1. **不阻塞事件循环**：数据库查询是同步阻塞调用，直接在 async 生成器里执行会
       卡死整个 event loop（在此期间所有并发请求都被拖住）。这里用
       `asyncio.to_thread` 把它丢到线程池执行。
    2. **客户端断连即退出**：每轮检查 `request.is_disconnected()`，避免浏览器关闭
       后生成器继续空转、连接与 DB 会话长期泄漏。
    """

    async def _snapshot() -> dict:
        def _query() -> dict:
            db = SessionLocal()
            try:
                return task_service.get_status(db, task_id)
            finally:
                db.close()

        return await asyncio.to_thread(_query)

    async def event_gen():
        last_snapshot = ""
        elapsed = 0.0
        while True:
            if await request.is_disconnected():
                logger.debug(f"任务 {task_id} 的 SSE 客户端已断开，结束进度流")
                break
            status = await _snapshot()
            snapshot = json.dumps(status, ensure_ascii=False)
            if snapshot != last_snapshot:
                yield f"data: {snapshot}\n\n"
                last_snapshot = snapshot
            # 终止态：成功 / 失败 / 被取消。缺少 cancelled 会导致取消后 while 死循环、连接泄漏
            if status["status"] in {"success", "failed", "cancelled"}:
                break
            if elapsed >= SSE_MAX_DURATION:
                logger.warning(f"任务 {task_id} 的 SSE 已超过 {SSE_MAX_DURATION:.0f}s，主动结束")
                break
            await asyncio.sleep(SSE_POLL_INTERVAL)
            elapsed += SSE_POLL_INTERVAL

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """取消一个生成任务（工作中任务将在下一节点中断）。"""
    task = task_service.cancel_task(db, task_id)
    return ok({"task_id": task.id, "status": task.status})


@router.post("/{task_id}/regenerate", dependencies=[Depends(heavy_rate_limit)])
def regenerate(
    task_id: int,
    req: RegenerateRequest,
    db: Session = Depends(get_db),
) -> dict:
    """局部重生成（仅文案 / 仅图片）。"""
    task, edited_copy = task_service.schedule_regenerate(db, task_id, req)
    _submit_task(
        task_service.run_task, task.id, edited_copy, visual_hint=req.visual_hint
    )
    return ok({"task_id": task.id, "mode": req.mode})
