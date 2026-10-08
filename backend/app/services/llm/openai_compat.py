"""OpenAI 兼容协议客户端。

通过配置 base_url 可对接 GPT-4o / Claude / Qwen-VL / Gemini / Kimi(Moonshot) 等
所有暴露 OpenAI 兼容 /chat/completions 端点的服务。

针对不同上游的协议差异做了适配：
- temperature 锁定：部分推理型模型（kimi-k3 / kimi-k2.x）只接受 temperature=1，
  传其他值直接 400。客户端会自动省略该参数并提示一次。
- 思考模式：kimi-k2.x 支持 thinking.type=enabled/disabled；kimi-k3 用 reasoning_effort。
- 限流：上游账号常有组织级 RPM 上限（Kimi 新账号默认仅 3），
  流水线连续调用必然触发 429，故内置客户端限流器 + Retry-After 退避。
"""
from __future__ import annotations

import base64
import threading
import time
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings
from app.core.metrics import record_llm_call
from app.services.llm.base import LLMClient, track_usage
from app.services.llm.pricing import estimate_cost

# 各提供商默认 OpenAI 兼容端点
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "claude": "https://api.anthropic.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "kimi": "https://api.moonshot.cn/v1",
}

# 只接受 temperature=1 的模型前缀（传其他值会 400 invalid_request_error）
_TEMPERATURE_LOCKED_PREFIXES = ("kimi-k", "moonshot-v1", "o1", "o3", "o4")

# 单次调用的最大尝试次数（含首次），用于 429 / 5xx / 网络抖动
_MAX_ATTEMPTS = 6
# 单次调用的**总重试预算**（秒）。重试等待（Retry-After / 限流间隔 / 指数退避）
# 各级叠加可能高达每次 ~1min；若不设总预算，一次调用最坏可挂 5min+，在 8-12 次
# 串行调用的生成流水线里会叠加成不可接受的卡顿。到达预算即放弃重试、立刻抛错。
_MAX_RETRY_BUDGET_SEC = 60.0


def _parse_retry_after(header: str) -> float | None:
    """解析 Retry-After 头，同时支持**秒数**与 **HTTP-date** 两种合法格式。

    仅用 `isdigit()` 判断会漏掉 `Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`
    （RFC 7231 允许的另一种形式），此时会错误地退回指数退避，等待时间与上游要求不符。
    """
    if header.isdigit():
        return float(header)
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    from datetime import datetime, timezone

    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max((target - datetime.now(timezone.utc)).total_seconds(), 0.0)


class OpenAICompatLLM(LLMClient):
    """通用 OpenAI 协议客户端。"""

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str = "",
        timeout: int = 60,
        vision_model: str = "",
        max_rpm: int = 0,
        thinking: str = "",
        reasoning_effort: str = "",
    ) -> None:
        self.provider = provider
        self.model = model
        self.vision_model = vision_model or model
        self.api_key = api_key
        self.base_url = (
            base_url or DEFAULT_BASE_URLS.get(provider, "https://api.openai.com/v1")
        ).rstrip("/")
        self.timeout = timeout
        self.max_rpm = max(0, int(max_rpm or 0))
        self.thinking = (thinking or "").strip().lower()
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        self._temperature_warned = False
        # 限流状态：调用间隔 = 60 / max_rpm，串行化以杜绝瞬时并发超限
        self._rate_lock = threading.Lock()
        self._last_call_at = 0.0
        # 复用一个 Client 以复用 TCP 连接（连接池），避免每次调用重新握手
        self._client = httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    # ---------- 上游差异适配 ----------

    def _throttle(self) -> None:
        """客户端限流：保证相邻请求间隔不小于 60/max_rpm 秒。"""
        if self.max_rpm <= 0:
            return
        interval = 60.0 / self.max_rpm
        with self._rate_lock:
            wait = self._last_call_at + interval - time.monotonic()
            if wait > 0:
                logger.debug(f"LLM 限流等待 {wait:.1f}s（LLM_MAX_RPM={self.max_rpm}）")
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    def _reset_throttle(self) -> None:
        """收到 429 后重置计时，让下一个请求重新等待完整间隔。"""
        with self._rate_lock:
            self._last_call_at = time.monotonic()

    def _temperature_supported(self, model: str) -> bool:
        m = (model or "").lower()
        return not any(m.startswith(p) for p in _TEMPERATURE_LOCKED_PREFIXES)

    def _apply_model_options(self, payload: dict[str, Any], model: str) -> None:
        """注入思考模式 / 推理强度等模型专属参数（仅对支持的模型注入，避免 400）。"""
        m = (model or "").lower()
        if self.thinking and m.startswith("kimi-k2"):
            payload["thinking"] = {"type": self.thinking}
        if self.reasoning_effort and m.startswith("kimi-k3"):
            payload["reasoning_effort"] = self.reasoning_effort

    @staticmethod
    def _retry_after(resp: httpx.Response, max_rpm: int, attempt: int) -> float:
        """计算重试等待秒数：优先用服务端 Retry-After，其次按限流间隔，最后指数退避。"""
        header = (resp.headers.get("Retry-After") or "").strip()
        if header:
            seconds = _parse_retry_after(header)
            if seconds is not None:
                return min(seconds, 60.0)
        if max_rpm > 0:
            return min(max(60.0 / max_rpm, 1.0), 60.0)
        return float(min(2 ** attempt, 30))

    # ---------- 请求 ----------

    def _call(self, payload: dict[str, Any], model: str, purpose: str = "") -> dict[str, Any]:
        """发起一次对话补全请求（内置限流、429 退避与温度锁定自愈）。"""
        url = f"{self.base_url}/chat/completions"
        last_error: Exception | None = None
        budget_start = time.monotonic()
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            # 总重试预算守门：即使还剩尝试次数，也不再为一次调用无限等待
            if time.monotonic() - budget_start >= _MAX_RETRY_BUDGET_SEC:
                break
            self._throttle()
            started = time.time()
            try:
                resp = self._client.post(url, json=payload, headers=self._headers())
            except httpx.TransportError as exc:  # 网络抖动 / 超时
                last_error = exc
                wait = float(min(2 ** attempt, 20))
                logger.warning(f"LLM 网络异常（{type(exc).__name__}），{wait:.0f}s 后重试")
                time.sleep(wait)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:200]}", request=resp.request, response=resp
                )
                if resp.status_code == 429:
                    self._reset_throttle()
                wait = self._retry_after(resp, self.max_rpm, attempt)
                logger.warning(
                    f"LLM 返回 {resp.status_code}，{wait:.0f}s 后重试（第 {attempt}/{_MAX_ATTEMPTS} 次）"
                )
                time.sleep(wait)
                continue

            # temperature 锁定自愈：省略该参数后重试（不额外等待）
            if (
                resp.status_code == 400
                and "temperature" in resp.text
                and "temperature" in payload
            ):
                payload.pop("temperature", None)
                if not self._temperature_warned:
                    self._temperature_warned = True
                    logger.warning(
                        f"模型 {model} 不接受自定义 temperature（仅允许 1），"
                        f"已自动省略该参数（LLM_TEMPERATURE 对该模型不生效）"
                    )
                continue

            resp.raise_for_status()
            data = resp.json()
            self._record_usage(data, model, purpose, started)
            return data

        raise last_error or RuntimeError("LLM 调用失败：已达最大重试次数")

    def _record_usage(
        self, data: dict[str, Any], model: str, purpose: str, started: float
    ) -> None:
        """记录 token 用量与估算成本（失败不影响主流程）。"""
        try:
            latency_ms = int((time.time() - started) * 1000)
            usage = data.get("usage", {}) or {}
            tokens_in = usage.get("prompt_tokens", 0) or 0
            tokens_out = usage.get("completion_tokens", 0) or 0
            cost = estimate_cost(model, tokens_in, tokens_out)
            track_usage(
                purpose=purpose,
                provider=self.provider,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost=cost,
            )
            record_llm_call(purpose, latency_ms, tokens_in, tokens_out, cost)
        except Exception:  # noqa: BLE001  统计失败不应中断业务
            pass

    def _complete(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None,
        json_mode: bool,
        purpose: str,
        model: str | None = None,
    ) -> str:
        use_model = model or self.model
        payload: dict[str, Any] = {"model": use_model, "messages": messages}
        temp = temperature if temperature is not None else settings.llm_temperature
        if self._temperature_supported(use_model):
            payload["temperature"] = temp
        elif temp != 1 and not self._temperature_warned:
            self._temperature_warned = True
            logger.warning(
                f"模型 {use_model} 仅接受 temperature=1，已忽略配置值 {temp}"
            )
        self._apply_model_options(payload, use_model)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            data = self._call(payload, use_model, purpose)
            return data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            logger.error(f"LLM call failed: {exc}")
            raise

    # ---------- 对外接口 ----------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        return self._complete(messages, temperature, json_mode, purpose)

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
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image_mime};base64,{b64}"},
                    },
                ],
            }
        ]
        return self._complete(messages, None, json_mode, purpose, model=self.vision_model)
