"""LangGraph 状态定义。"""
from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """贯穿整个编排流程的状态。"""

    # 输入参数
    task_id: int
    product_id: int
    country: str
    language: str
    platform: str
    style: str
    price: str
    promotion: str
    size_preset: str
    num_versions: int

    # 中间结果
    product: dict[str, Any]
    market: dict[str, Any]
    # 卖点提炼结果（{product_type, audience, scenario, core_points[...]}）
    core_points: dict[str, Any]
    copies: dict[str, Any]
    compliance: dict[str, Any]
    design: dict[str, Any]
    images: list[dict[str, Any]]

    # 重试计数
    retry_count: int

    # 局部重生成：注入已编辑文案，跳过 LLM 文案生成
    edited_copy: dict[str, Any]

    # 用户在产品卡片上的手工修正（叠加在识别结果之上）
    product_override: dict[str, Any]

    # 视觉重生成：用户对背景/风格/色调的额外要求（注入 prompt_gen 提示词）
    visual_hint: str
