"""背景提示词安全兜底。

架构是「AI 生成背景 + 真实产品抠图居中合成」，背景中央出现大型实物
（沙发/床/椅等）会导致产品像"搁在沙发上"一样违和。LLM 违规时在此拦截替换
为安全棚拍背景，并合并强制 negative prompt 词表。
"""
from __future__ import annotations

from loguru import logger

BLOCKED_BG_TOKENS = (
    "sofa", "couch", "armchair", "furniture", "chair", "bed",
    "curtain", "bookshelf", "cabinet", "wardrobe", "fireplace",
)

SAFE_BG_PROMPT = (
    "clean minimalist studio backdrop, soft diffused professional lighting, "
    "subtle gradient seamless background, large empty clean space in the center "
    "of the frame for product placement, premium commercial photography, "
    "photorealistic, sharp details, 8k quality"
)

MANDATORY_NEGATIVES = (
    "product, people, person, human, face, hand, text, letters, numbers, "
    "watermark, logo, brand, sofa, couch, bed, chair, furniture, window, "
    "curtains, plant, extra objects, duplicate, distorted, blurry, low quality"
)


def ensure_safe_bg_prompt(prompt: str) -> str:
    """背景提示词含违禁实物词时，整体替换为安全棚拍背景。"""
    p = (prompt or "").lower()
    if any(tok in p for tok in BLOCKED_BG_TOKENS):
        logger.warning(
            f"背景提示词含不适配实物（沙发/家具等），替换为安全棚拍背景: {prompt[:60]}"
        )
        return SAFE_BG_PROMPT
    return prompt


def merge_negatives(neg: str) -> str:
    """合并强制 negative prompt 词表（防 LLM 漏写）。"""
    neg = (neg or "").strip()
    existing = {t.strip().lower() for t in neg.split(",") if t.strip()}
    missing = [
        t for t in MANDATORY_NEGATIVES.split(", ")
        if t.lower() not in existing
    ]
    if not missing:
        return neg
    return f"{neg}, {', '.join(missing)}".strip(", ") if neg else ", ".join(missing)
