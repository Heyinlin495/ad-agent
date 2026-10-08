"""Agent 工具集（可被节点与 LangGraph 工具调用复用）。"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from app.prompts import render_prompt
from app.schemas.ad import ComplianceReport
from app.services.llm import get_llm


def rag_search(
    query: str,
    top_k: int = 5,
    country: str | None = None,
    platform: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """检索知识库（混合检索），返回带引用来源的结果列表。"""
    from app.core.database import SessionLocal
    from app.services.kb_service import search

    db = SessionLocal()
    try:
        result = search(db, query, top_k, country, platform, category)
        return [s.model_dump() for s in result.sources]
    finally:
        db.close()


def compliance_check(
    copy_text: str,
    country: str,
    platform: str,
    compliance_rules: list[str],
) -> ComplianceReport:
    """调用 LLM 审查文案合规性。

    合规审查**失败不应打死整条流水线**：LLM 返回非 JSON、漏字段（pydantic
    ValidationError）、网络异常等情况下，降级为"放行 + 标记未审查"，由下游
    在结果里带出合规标记，交由人工复核。
    """
    llm = get_llm()
    prompt = render_prompt(
        "compliance_check",
        country=country,
        platform=platform,
        copy_text=copy_text,
        compliance_rules="\n".join(f"- {r}" for r in compliance_rules),
    )
    try:
        result = llm.chat_json(
            [{"role": "user", "content": prompt}],
            purpose="compliance_check",
        )
        return ComplianceReport.model_validate(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"合规审查失败，降级为放行（标记待人工复核）: {exc}")
        return ComplianceReport(
            passed=True,
            violations=[],
            feedback=f"合规审查未执行（{type(exc).__name__}），建议人工复核。",
        )


def translate(
    text: str, target_language: str, source_language: str = "en"
) -> str:
    """多语言本地化翻译。"""
    llm = get_llm()
    prompt = render_prompt(
        "translate",
        text=text,
        target_language=target_language,
        source_language=source_language,
    )
    result = llm.chat_json(
        [{"role": "user", "content": prompt}],
        purpose="translate",
    )
    return result.get("translated", text)


def image_analyze(image_bytes: bytes, image_mime: str, hint: str = "") -> dict[str, Any]:
    """多模态识别（工具形态，供需要重新识别的场景使用）。"""
    from app.prompts import render_prompt as rp
    from app.services.llm import get_llm as _llm

    prompt = rp("product_analysis", hint=hint or "无")
    return _llm().vision_json(image_bytes, image_mime, prompt, purpose="product_analysis")


def image_generate(
    prompt: str,
    size: str,
    negative_prompt: str | None = None,
) -> bytes:
    """生成场景化背景图，返回图片字节。"""
    from app.image.generator import generate_image

    return generate_image(prompt, size, negative_prompt)


def image_edit(
    base_image_bytes: bytes,
    instruction: str,
    size: str,
) -> bytes:
    """基于原图的图像编辑（图生图），返回图片字节。失败时抛异常由调用方降级。"""
    from app.image.generator import edit_image

    return edit_image(base_image_bytes, instruction, size)


def compose_from_generated(
    base_image_bytes: bytes,
    design: dict[str, Any],
    headline: str,
    subheadline: str,
    cta: str,
    size_preset: str,
    bullets: list[str] | None = None,
) -> str:
    """图生图模式合成：编辑生成的整图作为广告画面 + 文字避让叠加，返回图片 URL。"""
    from app.image.composer import compose_ad_from_image

    return compose_ad_from_image(
        base_image_bytes,
        design,
        headline,
        subheadline,
        cta,
        size_preset,
        bullets=bullets,
    )


def layout_compose(
    subject_image_url: str,
    background_bytes: bytes | None,
    design: dict[str, Any],
    headline: str,
    subheadline: str,
    cta: str,
    size_preset: str,
    bullets: list[str] | None = None,
) -> str:
    """版式合成：主体图 + 背景 + 文案，返回图片 URL。"""
    from app.image.composer import compose_ad_image

    return compose_ad_image(
        subject_image_url,
        background_bytes,
        design,
        headline,
        subheadline,
        cta,
        size_preset,
        bullets=bullets,
    )
