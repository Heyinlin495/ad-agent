"""生产环境缺密钥时应硬失败，而非静默回退 mock。

覆盖：LLM 客户端工厂与 Embedding 提供商在 is_production 下的缺 key 行为。
"""
from __future__ import annotations

import pytest

from app.core.config import settings as real_settings


@pytest.fixture(autouse=True)
def _reset_caches():
    """每个用例前清空 lru_cache，避免单例串扰，并还原 environment。"""
    saved_env = real_settings.environment
    saved_mock = real_settings.mock_mode
    saved_llm = real_settings.llm_provider
    saved_llm_key = real_settings.llm_api_key
    saved_embed = real_settings.embedding_provider
    saved_ds_key = real_settings.dashscope_api_key
    try:
        from app.rag.embedding import get_embedding
        from app.services.llm.factory import get_llm

        get_llm.cache_clear()
        get_embedding.cache_clear()
        yield
    finally:
        real_settings.environment = saved_env
        real_settings.mock_mode = saved_mock
        real_settings.llm_provider = saved_llm
        real_settings.llm_api_key = saved_llm_key
        real_settings.embedding_provider = saved_embed
        real_settings.dashscope_api_key = saved_ds_key
        get_llm.cache_clear()
        get_embedding.cache_clear()


def _llm():
    from app.services.llm.factory import get_llm

    return get_llm()


def test_llm_production_missing_key_raises():
    from app.services.llm.factory import LLMConfigError

    real_settings.environment = "production"
    real_settings.mock_mode = False
    real_settings.llm_provider = "openai"
    real_settings.llm_api_key = ""

    with pytest.raises(LLMConfigError):
        _llm()


def test_llm_dev_missing_key_falls_back_mock():
    from app.services.llm.mock import MockLLM

    real_settings.environment = "development"
    real_settings.mock_mode = False
    real_settings.llm_provider = "openai"
    real_settings.llm_api_key = ""

    assert isinstance(_llm(), MockLLM)


def test_llm_provider_mock_always_fine():
    from app.services.llm.mock import MockLLM

    real_settings.llm_provider = "mock"
    real_settings.environment = "production"

    assert isinstance(_llm(), MockLLM)


def test_embedding_production_missing_key_raises():
    from app.rag.embedding import get_embedding

    real_settings.environment = "production"
    real_settings.embedding_provider = "dashscope"
    real_settings.dashscope_api_key = ""

    with pytest.raises(RuntimeError):
        get_embedding()


def test_embedding_dev_falls_back_mock():
    from app.rag.embedding import MockEmbedding, get_embedding

    real_settings.environment = "development"
    real_settings.embedding_provider = "dashscope"
    real_settings.dashscope_api_key = ""

    assert isinstance(get_embedding(), MockEmbedding)