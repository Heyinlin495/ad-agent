"""LLM 客户端抽象层。

提供统一的文本 / 多模态调用接口，底层可切换：
- openai (GPT-4o / 兼容 OpenAI 协议的任何服务)
- claude (通过 Anthropic OpenAI 兼容端点)
- qwen (通义千问 DashScope 兼容端点)
- gemini (Google Gemini OpenAI 兼容端点)
- kimi (月之暗面 Moonshot Kimi，https://api.moonshot.cn/v1)
- mock (无 Key 演示)
"""
from .base import LLMClient
from .factory import get_llm

__all__ = ["LLMClient", "get_llm"]
