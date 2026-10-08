"""OpenAI 兼容客户端的上游差异适配测试（不发起真实网络请求）。

覆盖：
- 推理型模型（kimi-k3 / kimi-k2.x）的 temperature 锁定处理
- 思考模式 / 推理强度参数只注入给支持的模型
- 自定义字段 _purpose 不再随请求发往第三方接口
- 429 限流自动退避重试
- 400（temperature 不被接受）自愈重试
- 客户端限流器按 LLM_MAX_RPM 拉长请求间隔
"""
from __future__ import annotations

import pytest

from app.services.llm import openai_compat as mod
from app.services.llm.openai_compat import OpenAICompatLLM


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}
        self.request = None

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status for {self.status_code}")


class RecordingClient:
    """记录每次请求 payload，并按脚本返回响应。"""

    def __init__(self, script: list[FakeResponse]):
        self.script = list(script)
        self.payloads: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002  与 httpx 签名保持一致
        self.payloads.append(dict(json or {}))
        if self.script:
            return self.script.pop(0)
        return _ok_response()


def _ok_response(content: str = '{"ok": true}') -> FakeResponse:
    return FakeResponse(
        200,
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def make_client(script=None, **kwargs) -> OpenAICompatLLM:
    client = OpenAICompatLLM(
        provider=kwargs.pop("provider", "kimi"),
        model=kwargs.pop("model", "kimi-k2.6"),
        api_key="sk-test",
        base_url=kwargs.pop("base_url", ""),
        timeout=kwargs.pop("timeout", 30),
        **kwargs,
    )
    client._client = RecordingClient(script or [])
    return client


@pytest.fixture
def no_sleep(monkeypatch):
    """把模块内的 time 换成假实现，避免测试真实等待。"""

    class FakeTime:
        def __init__(self):
            self.slept: list[float] = []
            self._now = 1000.0

        def monotonic(self) -> float:
            return self._now

        def time(self) -> float:
            return self._now

        def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self._now += seconds

    fake = FakeTime()
    monkeypatch.setattr(mod, "time", fake)
    return fake


# ---------------- 端点默认值 ----------------

def test_kimi_default_base_url():
    c = OpenAICompatLLM(provider="kimi", model="kimi-k2.6", api_key="sk-x")
    assert c.base_url == "https://api.moonshot.cn/v1"


def test_explicit_base_url_wins():
    c = OpenAICompatLLM(
        provider="kimi", model="kimi-k2.6", api_key="sk-x",
        base_url="https://api.moonshot.cn/v1/",
    )
    assert c.base_url == "https://api.moonshot.cn/v1"


# ---------------- temperature 锁定 ----------------

def test_temperature_omitted_for_kimi(no_sleep):
    """kimi-k2.6 / kimi-k3 只接受 temperature=1，客户端应省略该参数。"""
    for model in ("kimi-k2.6", "kimi-k3"):
        c = make_client(model=model)
        c.chat([{"role": "user", "content": "hi"}], temperature=0.3)
        assert "temperature" not in c._client.payloads[0], model


def test_temperature_sent_for_openai(no_sleep):
    """非锁定模型仍按配置下发 temperature。"""
    c = make_client(provider="openai", model="gpt-4o", base_url="https://api.openai.com/v1")
    c.chat([{"role": "user", "content": "hi"}], temperature=0.3)
    assert c._client.payloads[0]["temperature"] == 0.3


def test_temperature_400_self_heal(no_sleep):
    """上游若仍报 temperature 非法，去掉该参数自动重试并成功。"""
    bad = FakeResponse(
        400,
        text='{"error":{"message":"invalid temperature: only 1 is allowed for this model"}}',
    )
    c = make_client(provider="openai", model="gpt-4o", script=[bad, _ok_response()])
    out = c.chat([{"role": "user", "content": "hi"}], temperature=0.2)
    assert out == '{"ok": true}'
    assert len(c._client.payloads) == 2
    assert "temperature" in c._client.payloads[0]
    assert "temperature" not in c._client.payloads[1]


# ---------------- 思考模式 / 推理强度 ----------------

def test_thinking_injected_for_kimi_k2(no_sleep):
    c = make_client(model="kimi-k2.6", thinking="disabled")
    c.chat([{"role": "user", "content": "hi"}])
    assert c._client.payloads[0]["thinking"] == {"type": "disabled"}


def test_thinking_not_injected_for_k3(no_sleep):
    """kimi-k3 不支持 thinking 参数，注入会 400。"""
    c = make_client(model="kimi-k3", thinking="disabled")
    c.chat([{"role": "user", "content": "hi"}])
    assert "thinking" not in c._client.payloads[0]


def test_reasoning_effort_only_for_k3(no_sleep):
    c3 = make_client(model="kimi-k3", reasoning_effort="low")
    c3.chat([{"role": "user", "content": "hi"}])
    assert c3._client.payloads[0]["reasoning_effort"] == "low"

    c26 = make_client(model="kimi-k2.6", reasoning_effort="low")
    c26.chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in c26._client.payloads[0]


def test_thinking_never_leaks_to_other_providers(no_sleep):
    c = make_client(provider="qwen", model="qwen-flash", thinking="disabled",
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    c.chat([{"role": "user", "content": "hi"}])
    assert "thinking" not in c._client.payloads[0]


# ---------------- 请求体洁净度 ----------------

def test_purpose_not_sent_upstream(no_sleep):
    """_purpose 是本地统计字段，不能发给第三方接口（严格服务端会 400）。"""
    c = make_client()
    c.chat([{"role": "user", "content": "hi"}], json_mode=True, purpose="market_strategy")
    payload = c._client.payloads[0]
    assert "_purpose" not in payload
    assert payload["response_format"] == {"type": "json_object"}


def test_vision_uses_vision_model(no_sleep):
    c = make_client(model="kimi-k2.6", vision_model="kimi-k2.6")
    c.vision(b"\x89PNG", "image/png", "描述这张图", json_mode=True, purpose="vision_test")
    payload = c._client.payloads[0]
    assert payload["model"] == "kimi-k2.6"
    content = payload["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


# ---------------- 限流与重试 ----------------

def test_429_retries_with_backoff(no_sleep):
    """429 应自动退避重试，而不是直接失败。"""
    limited = FakeResponse(429, text='{"error":"rate_limit_reached_error"}',
                           headers={"Retry-After": "2"})
    c = make_client(script=[limited, limited, _ok_response()])
    out = c.chat([{"role": "user", "content": "hi"}])
    assert out == '{"ok": true}'
    assert len(c._client.payloads) == 3
    assert no_sleep.slept == [2.0, 2.0]


def test_429_backoff_falls_back_to_rpm_interval(no_sleep):
    """无 Retry-After 时按 LLM_MAX_RPM 推算等待（3 RPM → 20s）。"""
    limited = FakeResponse(429, text='{"error":"rate_limit_reached_error"}')
    c = make_client(script=[limited, _ok_response()], max_rpm=3)
    c.chat([{"role": "user", "content": "hi"}])
    assert 20.0 in no_sleep.slept


def test_rate_limiter_spaces_calls(no_sleep):
    """LLM_MAX_RPM 生效：连续调用之间必须等待完整间隔。"""
    c = make_client(max_rpm=120)  # 间隔 0.5s
    c.chat([{"role": "user", "content": "a"}])
    c.chat([{"role": "user", "content": "b"}])
    assert no_sleep.slept == [0.5]


def test_no_rate_limit_by_default(no_sleep):
    c = make_client(max_rpm=0)
    c.chat([{"role": "user", "content": "a"}])
    c.chat([{"role": "user", "content": "b"}])
    assert no_sleep.slept == []


# ---------------- Anthropic 客户端限流（#7） ----------------

from app.services.llm import anthropic as anthropic_mod  # noqa: E402


class _AnthropicRecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002
        self.payloads.append(dict(json or {}))
        return self.responses.pop(0) if self.responses else _anthropic_ok()


def _anthropic_ok():
    return FakeResponse(
        200,
        {
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    )


@pytest.fixture
def claude_no_sleep(monkeypatch):
    """给 anthropic 模块换上假时间，避免真实等待。"""

    class FakeTime:
        def __init__(self):
            self.slept: list[float] = []
            self._now = 1000.0

        def monotonic(self):
            return self._now

        def time(self):
            return self._now

        def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self._now += seconds

    fake = FakeTime()
    monkeypatch.setattr(anthropic_mod, "time", fake)
    return fake


def test_anthropic_rpm_throttles_calls(claude_no_sleep):
    """Anthropic 客户端应按 LLM_MAX_RPM 拉长相邻调用间隔（与 OpenAI 客户端一致）。"""
    from app.services.llm.anthropic import AnthropicLLM

    c = AnthropicLLM(model="claude-3", api_key="sk-x", max_rpm=120)  # 间隔 0.5s
    c._client = _AnthropicRecordingClient([])
    c.chat([{"role": "user", "content": "a"}])
    c.chat([{"role": "user", "content": "b"}])
    assert claude_no_sleep.slept == [0.5]


def test_anthropic_no_throttle_by_default(claude_no_sleep):
    from app.services.llm.anthropic import AnthropicLLM

    c = AnthropicLLM(model="m-1", api_key="sk-x", max_rpm=0)
    c._client = _AnthropicRecordingClient([])
    c.chat([{"role": "user", "content": "a"}])
    c.chat([{"role": "user", "content": "b"}])
    assert claude_no_sleep.slept == []
