"""LLM 客户端基类与工具函数。"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from app.services.llm.usage import log_usage

# 代码块包裹：```json ... ``` / ``` ... ```
_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _balanced_json_object(text: str) -> str | None:
    """扫描出第一个**括号配对**的 JSON 对象子串（正确跳过字符串内的花括号与转义）。

    `text[find("{"):rfind("}")]` 这种写法在输出里含多个 JSON 片段、或注释/说明中
    出现花括号时会截出非法片段。这里按字符扫描并跟踪字符串状态，稳健得多。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json(text: str) -> dict[str, Any]:
    """从 LLM 返回文本中稳健地提取 JSON 对象。

    依次尝试：原文 → 去掉 ``` 代码块包裹 → 平衡括号截取第一个完整对象。
    任一步成功即返回；全部失败抛 ValueError（而非底层 JSONDecodeError），
    让上层降级逻辑能统一捕获并给出可读原因。
    """
    if not text or not text.strip():
        raise ValueError("LLM 返回内容为空，无法解析 JSON")

    candidates: list[str] = [text.strip()]

    # ```json ... ``` 包裹（可能有多段，取第一段）
    m = _CODE_FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())

    # 平衡括号截取（应对前后带说明文字的情况）
    balanced = _balanced_json_object(text)
    if balanced:
        candidates.append(balanced)

    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        # 顶层是数组时取第一个对象（部分模型会把单对象包成 [{}]）
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]

    raise ValueError(f"无法从 LLM 返回中解析出 JSON 对象：{text[:200]!r}")


class LLMClient(ABC):
    """多模态 / 文本 LLM 统一客户端接口。"""

    provider: str = "base"
    model: str = ""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        """文本对话，返回字符串（json_mode=True 时返回 JSON 字符串）。"""

    @abstractmethod
    def vision(
        self,
        image_bytes: bytes,
        image_mime: str,
        prompt: str,
        *,
        json_mode: bool = False,
        purpose: str = "",
    ) -> str:
        """多模态调用：给定图片与提示词，返回字符串。"""

    # ---------- 便捷方法 ----------

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        purpose: str = "",
    ) -> dict[str, Any]:
        text = self.chat(messages, temperature=temperature, json_mode=True, purpose=purpose)
        return extract_json(text)

    def vision_json(
        self,
        image_bytes: bytes,
        image_mime: str,
        prompt: str,
        *,
        purpose: str = "",
    ) -> dict[str, Any]:
        text = self.vision(
            image_bytes, image_mime, prompt, json_mode=True, purpose=purpose
        )
        return extract_json(text)


def track_usage(
    purpose: str,
    provider: str,
    model: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    cost: float = 0.0,
    task_id: int | None = None,
) -> None:
    """记录一次 LLM 调用成本（非阻塞，忽略记录失败）。

    同时输出一条 INFO 结构化日志（provider/model/purpose/耗时/token/成本），
    便于按节点维度排障；成本落库失败不影响日志输出。
    """
    logger.info(
        "llm_call provider={provider} model={model} purpose={purpose} "
        "task_id={task_id} tokens_in={tokens_in} tokens_out={tokens_out} "
        "latency_ms={latency_ms} cost_usd={cost}",
        provider=provider,
        model=model,
        purpose=purpose,
        task_id=task_id,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        cost=round(cost, 6),
    )
    try:
        log_usage(
            task_id=task_id,
            provider=provider,
            model=model,
            purpose=purpose,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost=cost,
        )
    except Exception:
        pass
