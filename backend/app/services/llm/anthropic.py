"""Anthropic Messages API 客户端（Claude 原生协议）。

支持官方 api.anthropic.com 以及阿里云 MaaS 等兼容端点。
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any

import httpx
from loguru import logger

from app.core.metrics import record_llm_call
from app.services.llm.base import LLMClient, track_usage
from app.services.llm.openai_compat import _parse_retry_after
from app.services.llm.pricing import estimate_cost

# 单次请求最大尝试次数（含首次）
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
# 单次调用的总重试预算（秒）：避免重试叠加导致单次调用挂数十秒起
_MAX_RETRY_BUDGET_SEC = 60.0


class AnthropicLLM(LLMClient):
    provider = "claude"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout: int = 60,
        api_path: str = "/v1/messages",
        max_rpm: int = 0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.api_path = api_path
        self.timeout = timeout
        self.max_rpm = max(0, int(max_rpm or 0))
        # 客户端限流：与 OpenAI 兼容客户端一致的 RPM 节流（上游账号有组织级 RPM 上限时）
        self._rate_lock = threading.Lock()
        self._last_call_at = 0.0
        # 复用一个 Client 以复用 TCP 连接（连接池）
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _throttle(self) -> None:
        """客户端限流：保证相邻请求间隔不小于 60/max_rpm 秒（0 = 不限流）。

        与 OpenAI 兼容客户端一致：RPM 是**进程级串行**约束，多线程同时放行会击穿
        配额，因此在锁内等待并推进计时是正确做法（等待时间由 _MAX_RETRY_BUDGET 兜底）。
        """
        if self.max_rpm <= 0:
            return
        interval = 60.0 / self.max_rpm
        with self._rate_lock:
            wait = self._last_call_at + interval - time.monotonic()
            if wait > 0:
                logger.debug(f"Claude 限流等待 {wait:.1f}s（LLM_MAX_RPM={self.max_rpm}）")
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    @staticmethod
    def _wait_seconds(resp: httpx.Response, attempt: int) -> float:
        """重试等待：优先遵循上游 Retry-After，其次指数退避（上限 10s）。"""
        header = (resp.headers.get("Retry-After") or "").strip()
        if header:
            secs = _parse_retry_after(header)
            if secs is not None:
                return min(max(secs, 0.0), 30.0)
        return float(min(2 ** attempt, 10))

    def _request(self, payload: dict[str, Any], purpose: str) -> dict[str, Any]:
        """发起 Messages 请求，对 429 / 5xx 与网络抖动做带退避的重试。

        原实现用 tenacity 的指数退避，不读 Retry-After：上游明确要求等 N 秒时
        仍按固定节奏重试，既容易继续撞 429，又可能因间隔过短被判为滥用。
        """
        started = time.time()
        url = f"{self.base_url}{self.api_path}"
        last_error: Exception | None = None
        budget_start = time.monotonic()
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            # 总重试预算守门：即使还剩尝试次数，也不再为一次调用无限等待
            if time.monotonic() - budget_start >= _MAX_RETRY_BUDGET_SEC:
                break
            self._throttle()
            try:
                resp = self._client.post(url, json=payload, headers=self._headers())
            except httpx.TransportError as exc:
                last_error = exc
                wait = float(min(2 ** attempt, 10))
                logger.warning(
                    f"Claude 网络异常（{type(exc).__name__}），{wait:.0f}s 后重试"
                    f"（第 {attempt}/{_MAX_ATTEMPTS} 次）"
                )
                time.sleep(wait)
                continue

            if resp.status_code in _RETRYABLE_STATUS:
                last_error = httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:200]}",
                    request=resp.request,
                    response=resp,
                )
                wait = self._wait_seconds(resp, attempt)
                logger.warning(
                    f"Claude 返回 {resp.status_code}，{wait:.0f}s 后重试"
                    f"（第 {attempt}/{_MAX_ATTEMPTS} 次）"
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return self._record(resp.json(), started, purpose)

        raise last_error or RuntimeError("Claude 调用失败：已达最大重试次数")

    def _record(self, data: dict[str, Any], started: float, purpose: str) -> dict[str, Any]:
        usage = data.get("usage", {})
        tokens_in = usage.get("input_tokens", 0) or 0
        tokens_out = usage.get("output_tokens", 0) or 0
        latency_ms = int((time.time() - started) * 1000)
        cost = estimate_cost(self.model, tokens_in, tokens_out)
        track_usage(
            purpose=purpose,
            provider=self.provider,
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost=cost,
        )
        record_llm_call(purpose, latency_ms, tokens_in, tokens_out, cost)
        return data

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        return "".join(
            b.get("text", "")
            for b in data.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None,
        json_mode: bool,
    ) -> dict[str, Any]:
        system_parts: list[str] = []
        anthropic_msgs: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(content if isinstance(content, str) else str(content))
            else:
                anthropic_msgs.append({"role": role, "content": content})
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": anthropic_msgs,
        }
        if system_parts:
            payload["system"] = "\n".join(system_parts)
        if json_mode:
            payload["system"] = (payload.get("system", "") + "\n只输出 JSON，不要包含任何其他内容。").strip()
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        payload = self._build_payload(messages, temperature, json_mode)
        data = self._request(payload, purpose)
        return self._extract_text(data)

    def vision(
        self,
        image_bytes: bytes,
        image_mime: str,
        prompt: str,
        *,
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": image_mime, "data": b64},
            },
            {"type": "text", "text": prompt},
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": content}],
        }
        if json_mode:
            payload["system"] = "只输出 JSON，不要包含任何其他内容。"
        data = self._request(payload, purpose)
        return self._extract_text(data)
