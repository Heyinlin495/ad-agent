"""广告生成任务服务：创建、异步执行、状态查询、局部重生成。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.agents.graph import run_graph
from app.agents.state import AgentState
from app.core import metrics
from app.core.database import SessionLocal
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.signing import sign_file_url
from app.models import AdCopyVersion, AdImage, AdTask, Product
from app.schemas.ad import GenerateRequest, RegenerateRequest

# 进度落库节流：进度与节点均无变化时，至少间隔这么久才写一次库
# （兼作取消检查的心跳，保证取消请求能在数秒内被感知）
PROGRESS_HEARTBEAT_SEC = 3.0


class TaskCancelledError(Exception):
    """任务被用户取消。"""


def create_task(db: Session, req: GenerateRequest) -> AdTask:
    """创建任务并落库，返回待执行任务。"""
    product = db.get(Product, req.product_id)
    if product is None:
        raise NotFoundError(f"产品 {req.product_id} 不存在")
    task = AdTask(
        product_id=req.product_id,
        country=req.country,
        language=req.language,
        platform=req.platform,
        style=req.style,
        price=req.price,
        promotion=req.promotion,
        size_preset=req.size_preset,
        num_versions=max(1, min(int(req.num_versions or 3), 3)),
        # 产品卡片上的手工修正落库，生成时叠加到识别结果之上
        product_override=(
            req.product_override.model_dump(exclude_none=True)
            if req.product_override
            else None
        ),
        status="pending",
        progress=0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _build_initial_state(
    task: AdTask,
    edited_copy: dict[str, Any] | None = None,
    visual_hint: str | None = None,
) -> AgentState:
    state: AgentState = {
        "task_id": task.id,
        "product_id": task.product_id or 0,
        "country": task.country,
        "language": task.language,
        "platform": task.platform,
        "style": task.style,
        "price": task.price or "",
        "promotion": task.promotion or "",
        "size_preset": task.size_preset,
        # 使用任务创建时透传的版本数（缺列/旧数据兜底为 3）
        "num_versions": getattr(task, "num_versions", 3) or 3,
        "retry_count": 0,
    }
    if edited_copy is not None:
        state["edited_copy"] = edited_copy  # type: ignore[typeddict-item]
    if visual_hint:
        state["visual_hint"] = visual_hint  # type: ignore[typeddict-item]
    override = getattr(task, "product_override", None)
    if override:
        state["product_override"] = dict(override)  # type: ignore[typeddict-item]
    return state


def _update_task(task_id: int, **fields: Any) -> None:
    db = SessionLocal()
    try:
        task = db.get(AdTask, task_id)
        if task is not None:
            for k, v in fields.items():
                setattr(task, k, v)
            db.commit()
    finally:
        db.close()


def _finish_task(task_id: int, status: str, **fields: Any) -> bool:
    """以原子条件迁移任务到终态。

    只在任务仍处于非终态时写入：若用户在执行过程中点取消（cancel_task 已把
    status 置 cancelled），这里不会再用 success/failed 覆盖它，避免"取消被推翻"。
    返回是否成功写入终态。
    """
    db = SessionLocal()
    try:
        result = db.execute(
            update(AdTask)
            .where(AdTask.id == task_id, AdTask.status.notin_(("success", "failed", "cancelled")))
            .values(status=status, **fields)
        )
        db.commit()
        return result.rowcount > 0
    finally:
        db.close()


def run_task(
    task_id: int,
    edited_copy: dict[str, Any] | None = None,
    visual_hint: str | None = None,
) -> None:
    """在后台线程执行图，更新进度并持久化结果。

    取消与进度均以数据库为准（不再依赖进程内存），因此多 worker / 重启后
    取消标记依旧生效；进度单调递增由 `progress > task.progress` 保证，
    重试回退节点时 UI 不会倒退。
    """
    db = SessionLocal()
    try:
        task = db.get(AdTask, task_id)
        if task is None:
            return
        initial_state = _build_initial_state(task, edited_copy, visual_hint)
    finally:
        db.close()

    _update_task(task_id, status="running", error=None)

    last_ts = 0.0
    last_node = ""
    last_progress = -1

    def on_progress(node: str, progress: int) -> None:
        nonlocal last_ts, last_node, last_progress
        now = time.monotonic()
        # 节流：进度与节点都没变，且距上次写库不足心跳间隔 → 跳过写库
        if (
            progress == last_progress
            and node == last_node
            and (now - last_ts) < PROGRESS_HEARTBEAT_SEC
        ):
            return
        db2 = SessionLocal()
        try:
            t = db2.get(AdTask, task_id)
            if t is None:
                return
            if t.cancel_requested:
                raise TaskCancelledError("任务已被取消")
            if progress > t.progress:  # 单调递增，避免重试回退时 UI 回退
                t.progress = progress
            t.current_node = node
            db2.commit()
        finally:
            db2.close()
        last_ts, last_node, last_progress = now, node, progress

    started_at = time.monotonic()
    try:
        final = run_graph(initial_state, on_progress=on_progress)
        _persist_results(task_id, final)
        # 条件写入：若期间用户已取消，则保留 cancelled 不再覆盖为 success
        if _finish_task(task_id, "success", progress=100, current_node="image_compose"):
            metrics.record_task("success", int((time.monotonic() - started_at) * 1000))
        else:
            logger.info(f"任务 {task_id} 已完成但期间被取消，保留取消状态")
            metrics.record_task("cancelled", int((time.monotonic() - started_at) * 1000))
    except TaskCancelledError:
        logger.info(f"任务 {task_id} 已被用户取消")
        _update_task(task_id, status="cancelled", error="用户已取消")
        metrics.record_task("cancelled", int((time.monotonic() - started_at) * 1000))
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"任务 {task_id} 执行失败")
        if _finish_task(task_id, "failed", error=str(exc)):
            metrics.record_task("failed", int((time.monotonic() - started_at) * 1000))


def recover_orphan_tasks(db: Session, stale_after_sec: float | None = None) -> int:
    """服务启动时清理**本进程创建、且已长时间无心跳**的残留任务。

    为什么不能无差别地把所有 running/pending 标记为 failed：
    多 worker（uvicorn --workers N / 多副本）部署时，A 进程重启会把 B 进程**正在
    执行**的任务一起标成 failed，而这些任务其实还在正常跑，最终会被执行线程
    用 success 覆盖（或在取消路径上产生矛盾状态）。

    因此这里按「心跳新鲜度」判定：只回收 `updated_at` 已超过阈值的任务——
    真正死掉的任务不会再更新 `updated_at`，而在跑的任务每次进度落库都会刷新它
    （见 run_task 的进度心跳，间隔约 3s）。
    """
    threshold = PROGRESS_HEARTBEAT_SEC * 4 if stale_after_sec is None else stale_after_sec
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=threshold)
    result = db.execute(
        update(AdTask)
        .where(
            AdTask.status.in_(("running", "pending")),
            AdTask.updated_at < cutoff,
        )
        .values(
            status="failed",
            error="服务重启导致任务中断",
            cancel_requested=False,
        )
    )
    db.commit()
    count = result.rowcount or 0
    if count:
        logger.warning(f"启动清理：{count} 个超时无心跳的中断任务已标记为 failed")
    return count


def cancel_task(db: Session, task_id: int) -> AdTask:
    """标记任务为取消（工作中任务在下个进度回调中断）。

    用**条件 UPDATE** 做原子状态迁移，而非「读-改-写」：并发下（用户点取消
    与任务线程置 success / 重生成复位 cancel_requested 同时发生）读改写会互相
    覆盖，导致"取消被静默推翻"或"已完成任务被标成取消"。
    """
    task = db.get(AdTask, task_id)
    if task is None:
        raise NotFoundError(f"任务 {task_id} 不存在")
    # 只在非终态时置 cancelled，避免覆盖已 success/failed/cancelled 的任务
    db.execute(
        update(AdTask)
        .where(
            AdTask.id == task_id,
            AdTask.status.notin_(("success", "failed", "cancelled")),
        )
        .values(cancel_requested=True, status="cancelled")
    )
    # 终态任务也置 cancel_requested，让在途的进度回调尽快中断
    db.execute(
        update(AdTask)
        .where(AdTask.id == task_id)
        .values(cancel_requested=True)
    )
    db.commit()
    db.refresh(task)
    return task


def _persist_results(task_id: int, state: AgentState) -> None:
    db = SessionLocal()
    try:
        task = db.get(AdTask, task_id)
        if task is None:
            return
        # 清空旧结果
        for img in list(task.images):
            db.delete(img)
        for cv in list(task.copy_versions):
            db.delete(cv)

        copies = (state.get("copies") or {}).get("versions", [])
        # 广告策划 Agent 的主题落库（结果页展示）；局部重生成未重跑策划时沿用已有主题
        theme = str((state.get("copies") or {}).get("ad_theme") or task.ad_theme or "")
        task.ad_theme = theme[:255]
        for v in copies:
            db.add(
                AdCopyVersion(
                    task_id=task_id,
                    version_no=v.get("version_no", 0),
                    style_variant=v.get("style_variant", ""),
                    headline=v.get("headline", ""),
                    subheadline=v.get("subheadline", ""),
                    bullets=v.get("bullets", []),
                    cta=v.get("cta", ""),
                    platform_adaptations=v.get("platform_adaptations", {}),
                    hashtags=v.get("hashtags", []),
                    keywords=v.get("keywords", []),
                    rag_sources=v.get("rag_sources", []),
                    compliance=v.get("compliance", {}),
                )
            )
        for img in state.get("images", []):
            db.add(
                AdImage(
                    task_id=task_id,
                    copy_version_id=None,
                    image_url=img.get("image_url", ""),
                    template_id=img.get("template_id", ""),
                    size=img.get("size", ""),
                    scheme=img.get("scheme", {}),
                )
            )
        db.commit()
    finally:
        db.close()


def get_status(db: Session, task_id: int) -> dict[str, Any]:
    """查询任务状态与结果。

    关联的文案 / 图片改为**显式一次性加载**（selectinload）：原来靠 lazy relationship
    在属性访问时各发一条 SQL，`/stream` 每 0.5s 轮询一次就会反复触发（N+1 放大到
    "轮询次数 × 3" 条查询）。selectinload 只发固定 3 条，且与任务查询同批完成。
    """
    task = db.execute(
        select(AdTask)
        .where(AdTask.id == task_id)
        .options(
            selectinload(AdTask.copy_versions),
            selectinload(AdTask.images),
        )
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(f"任务 {task_id} 不存在")
    copies = [
        {
            "version_no": c.version_no,
            "style_variant": c.style_variant,
            "headline": c.headline,
            "subheadline": c.subheadline,
            "bullets": c.bullets,
            "cta": c.cta,
            "platform_adaptations": c.platform_adaptations,
            "hashtags": c.hashtags,
            "keywords": c.keywords,
            "rag_sources": c.rag_sources,
            "compliance": c.compliance,
        }
        for c in sorted(task.copy_versions, key=lambda x: x.version_no)
    ]
    images = [
        {
            "image_url": sign_file_url(i.image_url),
            "template_id": i.template_id,
            "size": i.size,
            "scheme": i.scheme,
        }
        for i in task.images
    ]
    return {
        "task_id": task.id,
        "status": task.status,
        "progress": task.progress,
        "current_node": task.current_node,
        "error": task.error,
        # 任务参数：历史详情页需要（此前缺失导致前端显示 "undefined · undefined"）
        "country": task.country,
        "language": task.language,
        "platform": task.platform,
        "style": task.style,
        "num_versions": getattr(task, "num_versions", 3) or 3,
        "ad_theme": getattr(task, "ad_theme", "") or "",
        "copies": copies,
        "images": images,
    }


def schedule_regenerate(
    db: Session, task_id: int, req: RegenerateRequest
) -> tuple[AdTask, dict[str, Any] | None]:
    """局部重生成：仅文案 / 仅图片（可携带编辑后的文案）。

    返回 (task, edited_copy)，edited_copy 用于注入跳过 LLM 文案生成。
    """
    task = db.get(AdTask, task_id)
    if task is None:
        raise NotFoundError(f"任务 {task_id} 不存在")

    edited_copy = req.edited_copy.model_dump() if req.edited_copy else None
    if req.mode == "image" and edited_copy is None and task.copy_versions:
        # 仅图片：沿用现有第一版文案，注入避免重复生成
        first = sorted(task.copy_versions, key=lambda x: x.version_no)[0]
        edited_copy = {
            "version_no": first.version_no,
            "style_variant": first.style_variant,
            "headline": first.headline,
            "subheadline": first.subheadline,
            "bullets": first.bullets,
            "cta": first.cta,
            "platform_adaptations": first.platform_adaptations,
            "hashtags": first.hashtags,
            "keywords": first.keywords,
            "rag_sources": first.rag_sources,
            "compliance": first.compliance,
        }

    # 原子 claim：仅当任务处于非执行态时才置回 pending。条件写在 WHERE 里，
    # 与 run_task 的置位形成互斥，避免"取消后又被重生成复活"的竞态。
    result = db.execute(
        update(AdTask)
        .where(
            AdTask.id == task_id,
            AdTask.status.notin_(("running",)),
            AdTask.cancel_requested.is_(False),
        )
        .values(status="pending", progress=0, error=None, cancel_requested=False)
    )
    if result.rowcount == 0:
        db.rollback()
        # 区分原因给出更准确的报错
        current = db.get(AdTask, task_id)
        if current is not None and current.status == "running":
            raise BadRequestError("任务正在执行中，无法重生成")
        raise BadRequestError("任务已被取消，无法重生成")
    db.commit()
    db.refresh(task)
    return task, edited_copy
