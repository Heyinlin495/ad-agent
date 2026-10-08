"""健康检查与信息接口。"""
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.core import metrics
from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.schemas.common import ok

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return ok(
        {
            "status": "ok",
            "app": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "mock_mode": settings.mock_mode,
            "time": datetime.utcnow().isoformat(),
        }
    )


@router.get("/settings")
def get_settings_info() -> dict:
    """暴露非敏感配置（API Key 不外泄）。"""
    return ok(
        {
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "vision_model": settings.vision_model,
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "vector_store_type": settings.vector_store_type,
            "image_provider": settings.image_provider,
            "rerank_provider": settings.rerank_provider,
            "mock_mode": settings.mock_mode,
            "storage_type": settings.storage_type,
            "max_upload_size_mb": settings.max_upload_size_mb,
        }
    )


@router.get("/metrics")
def get_metrics(request: Request) -> dict:
    """运行指标快照（任务成功率、LLM 调用与耗时、成本估算等）。

    配置 METRICS_TOKEN 后需携带 ?token= 或 X-Metrics-Token 头访问。
    """
    _require_metrics_token(request)
    return ok(metrics.snapshot())


@router.get("/metrics/prometheus")
def get_metrics_prometheus(request: Request) -> PlainTextResponse:
    """Prometheus 文本格式指标（可被 Prometheus 抓取 / Grafana datasource 消费）。

    与 /metrics 共用同一份进程内数据，仅输出格式不同。Auth 规则也一致：
    配置 METRICS_TOKEN 后需携带 ?token= 或 X-Metrics-Token 头。
    """
    _require_metrics_token(request)
    return PlainTextResponse(
        metrics.prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _require_metrics_token(request: Request) -> None:
    """校验指标接口的访问令牌（未配置则放行）。"""
    if settings.metrics_token:
        provided = request.query_params.get("token") or request.headers.get(
            "x-metrics-token", ""
        )
        if provided != settings.metrics_token:
            raise ForbiddenError("指标接口访问被拒绝")
