"""Mock LLM 客户端：无 API Key 演示用，返回确定性数据。"""
from __future__ import annotations

import time
from typing import Any

from app.services.llm.base import LLMClient, track_usage
from app.services.llm.mock_data import mock_response


class MockLLM(LLMClient):
    provider = "mock"
    model = "mock"

    def _respond(self, purpose: str) -> str:
        return mock_response(purpose)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        started = time.time()
        result = self._respond(purpose)
        track_usage(
            purpose=purpose or "chat",
            provider=self.provider,
            model=self.model,
            tokens_in=0,
            tokens_out=0,
            latency_ms=int((time.time() - started) * 1000),
            cost=0.0,
        )
        return result

    def vision(
        self,
        image_bytes: bytes,
        image_mime: str,
        prompt: str,
        *,
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        started = time.time()
        result = self._respond(purpose or "product_analysis")
        track_usage(
            purpose=purpose or "vision",
            provider=self.provider,
            model=self.model,
            tokens_in=0,
            tokens_out=0,
            latency_ms=int((time.time() - started) * 1000),
            cost=0.0,
        )
        return result
