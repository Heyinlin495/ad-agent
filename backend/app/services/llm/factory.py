"""LLM 客户端工厂。"""
from __future__ import annotations

from functools import lru_cache

from loguru import logger

from app.core.config import settings
from app.services.llm.anthropic import AnthropicLLM
from app.services.llm.base import LLMClient
from app.services.llm.mock import MockLLM
from app.services.llm.openai_compat import OpenAICompatLLM


class LLMConfigError(RuntimeError):
    """生产环境选择的真实 LLM provider 缺密钥。"""


def _resolve_fallback() -> LLMClient | None:
    """决定是否回退 MockLLM。

    返回 MockLLM 实例 = 应回退；返回 None = 使用真实 provider（密钥齐全）。
    """
    if settings.llm_provider == "mock":
        return MockLLM()
    if not settings.llm_api_key or not settings.llm_api_key.strip():
        if settings.is_production:
            raise LLMConfigError(
                f"生产环境已配置 LLM_PROVIDER={settings.llm_provider} 但 LLM_API_KEY 为空，"
                "拒绝静默回退 mock"
            )
        logger.warning(
            f"LLM_PROVIDER={settings.llm_provider} 未配置 LLM_API_KEY，回退 MockLLM（仅限非生产环境）"
        )
        return MockLLM()
    if settings.mock_mode:
        return MockLLM()
    return None  # 有密钥且未强制 mock → 按 provider 构造真实客户端


@lru_cache
def get_llm() -> LLMClient:
    """按配置返回 LLM 客户端实例（单例缓存，复用底层 HTTP 连接池）。

    开发环境无 Key 时返回 MockLLM 保证可演示；生产环境确认要真实 provider
    时缺密钥直接抛错，避免静默降级。
    """
    fallback = _resolve_fallback()
    if fallback is not None:
        return fallback
    if settings.llm_provider == "claude":
        return AnthropicLLM(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url or "https://api.anthropic.com",
            timeout=settings.llm_timeout,
            api_path=settings.llm_api_path,
            max_rpm=settings.llm_max_rpm,
        )
    return OpenAICompatLLM(
        provider=settings.llm_provider,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
        vision_model=settings.vision_model,
        max_rpm=settings.llm_max_rpm,
        thinking=settings.llm_thinking,
        reasoning_effort=settings.llm_reasoning_effort,
    )
