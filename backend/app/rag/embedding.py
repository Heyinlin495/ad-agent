"""Embedding 提供商（可配置：OpenAI / BGE-M3 / mock）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

import httpx
import numpy as np

from app.core.config import settings


class EmbeddingProvider(ABC):
    """向量化接口。"""

    dim: int = settings.embedding_dim

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文档。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """向量化查询。"""


class MockEmbedding(EmbeddingProvider):
    """确定性字符 n-gram 哈希向量（无外部依赖，供演示）。"""

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.embedding_dim

    def _embed(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float32)
        text = text.lower()
        for n in (1, 2, 3):
            for i in range(len(text) - n + 1):
                gram = text[i : i + n]
                vec[hash(gram) % self.dim] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class OpenAIEmbedding(EmbeddingProvider):
    """OpenAI / 兼容端点 Embedding（含阿里云 DashScope text-embedding）。"""

    def __init__(self, model: str | None = None, dimensions: int | None = None) -> None:
        self.model = model or settings.embedding_model
        # 独立于对话模型密钥：对话模型可切换为 Kimi 等第三方，Embedding 仍走 DashScope
        self.api_key = settings.dashscope_key
        self.base_url = (
            settings.embedding_base_url
            or (
                "https://dashscope.aliyuncs.com/compatible-mode/v1"
                if settings.embedding_provider == "dashscope"
                else settings.llm_base_url
            )
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.dim = settings.embedding_dim
        self.dimensions = dimensions
        # 复用一个 Client 以复用 TCP 连接（连接池）
        self._client = httpx.Client(timeout=60)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        payload: dict = {"model": self.model, "input": texts}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        resp = self._client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda x: x["index"])
        return [d["embedding"] for d in data]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


class BgeM3Embedding(EmbeddingProvider):
    """BGE-M3 本地嵌入（sentence-transformers，首次使用需下载模型）。"""

    def __init__(self, model: str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # 延迟导入

        self.model_name = model or settings.embedding_model
        self.encoder = SentenceTransformer(self.model_name)
        self.dim = self.encoder.get_sentence_embedding_dimension()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.encoder.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        ).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.encoder.encode(
            [text], normalize_embeddings=True, show_progress_bar=False
        ).tolist()[0]


@lru_cache
def get_embedding() -> EmbeddingProvider:
    """按配置返回 Embedding 提供商（单例缓存）。

    开发环境缺密钥时允许降级 mock（可演示）；生产环境显式选择了真实
    provider 却缺密钥时直接抛错，避免静默用假向量跑一次检索。
    """
    key = settings.dashscope_key
    want_remote = settings.embedding_provider in ("dashscope", "openai")

    if want_remote and not key:
        if settings.is_production:
            raise RuntimeError(
                f"生产环境已配置 EMBEDDING_PROVIDER={settings.embedding_provider} "
                "但未配置 DASHSCOPE_API_KEY，拒绝静默降级 mock"
            )
        return MockEmbedding()

    if settings.embedding_provider == "dashscope" and key:
        return OpenAIEmbedding(dimensions=settings.embedding_dim)
    if settings.embedding_provider == "openai" and key:
        return OpenAIEmbedding()
    if settings.embedding_provider == "bge-m3":
        try:
            return BgeM3Embedding()
        except Exception:  # noqa: BLE001  模型下载失败等，降级 mock
            return MockEmbedding()
    return MockEmbedding()
