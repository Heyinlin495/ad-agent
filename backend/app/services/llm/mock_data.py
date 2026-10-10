"""Mock 模式示例数据（无 API Key 演示用）。

各 purpose 返回确定性的、结构合法（可被对应 Pydantic Schema 校验通过）的 JSON。
"""
from __future__ import annotations

import json

# 示例产品识别结果
PRODUCT_ANALYSIS = {
    "category": "Consumer Electronics",
    "name": "Wireless Bluetooth Earbuds",
    "material": "ABS plastic + silicone",
    "color": "Matte White",
    "shape": "Compact in-ear buds with charging case",
    "scenes": ["commuting", "sports", "office", "travel"],
    "selling_points": [
        "Active Noise Cancellation",
        "30-hour total battery life",
        "IPX5 water resistance",
        "Bluetooth 5.3 low latency",
        "Ergonomic secure fit",
    ],
    "target_audience": "Young professionals and commuters aged 18-35",
    "brand_suspected": None,
    "risk_flags": [],
}

# 市场策略
MARKET_STRATEGY = {
    "product_type": "无线入耳式降噪耳机",
    "target_audience": "18-35 岁通勤族与学生党，重视降噪与续航，价格敏感但认参数",
    "usage_scenes": ["日常通勤", "健身运动", "在线会议", "旅行出差"],
    "point_priority": [
        "Active Noise Cancellation",
        "30-hour battery life",
        "IPX5 water resistance",
        "Bluetooth 5.3 low latency",
    ],
    "emotional_tone": "confident and energetic",
    "visual_style": "clean tech-product shot on gradient studio background",
    "cultural_notes": [
        "US consumers respond to direct benefit-led headlines",
        "Avoid overly technical jargon for mainstream audience",
    ],
    "compliance_rules": [
        "Do not claim 'best' or 'No.1' without substantiation",
        "Avoid medical claims about hearing",
    ],
    "rag_sources": [
        {
            "document_id": 1,
            "title": "US Market Compliance Rules",
            "content": "Avoid superlative claims such as 'best' or '#1' without evidence.",
            "score": 0.91,
            "metadata": {"country": "US", "platform": "Amazon"},
        }
    ],
}

# 三种风格变体
COPY_VARIANTS = [
    ("promo", "Silence the Noise, Own Your Day"),
    ("premium", "Studio-Grade Sound. Effortlessly Yours."),
    ("minimal", "Hear More. Carry Less."),
]


def _make_copy(version_no: int, style_variant: str, headline: str) -> dict:
    return {
        "version_no": version_no,
        "style_variant": style_variant,
        "headline": headline,
        "subheadline": "Wireless earbuds with Active Noise Cancellation and 30-hour battery life.",
        "bullets": [
            "Active Noise Cancellation|Blocks out the world so you hear only your music",
            "30-Hour Battery|All-day listening with fast USB-C charging",
            "IPX5 Waterproof|Sweat and rain resistant for every workout",
            "Bluetooth 5.3|Stable, low-latency wireless connection",
        ],
        "cta": "Shop Now",
        "platform_adaptations": {
            "amazon_bullets": [
                "ACTIVE NOISE CANCELLATION: Immerse yourself in music anywhere",
                "30-HOUR BATTERY: All-day listening with USB-C fast charge",
                "IPX5 WATERPROOF: Sweat and splash resistant",
                "BLUETOOTH 5.3: Stable low-latency connection",
                "ERGONOMIC FIT: Comfortable for hours",
            ],
            "google_rsa": {
                "headlines": [
                    "Wireless ANC Earbuds",
                    "30-Hour Battery Life",
                    "IPX5 Waterproof Earbuds",
                    "Bluetooth 5.3 Earbuds",
                    "Buy Wireless Earbuds Online",
                    "Noise Cancelling Earbuds",
                    "Comfortable In-Ear Headphones",
                    "Fast-Charging Earbuds",
                    "Premium Sound Earbuds",
                    "Wireless Earbuds for Sports",
                    "Lightweight Bluetooth Earbuds",
                    "Best Value ANC Earbuds",
                    "Shop Noise Cancelling Earbuds",
                    "Long Battery Life Earbuds",
                    "Sweatproof Wireless Earbuds",
                ],
                "descriptions": [
                    "Active Noise Cancellation and 30-hour battery in a compact design.",
                    "IPX5 water resistance for workouts, commuting and travel.",
                    "Bluetooth 5.3 with fast USB-C charging and secure fit.",
                    "Shop premium wireless earbuds with free shipping today.",
                ],
            },
            "tiktok_script": "Tired of loud commutes? These earbuds block the noise and last 30 hours. Tap to shop!",
        },
        "hashtags": ["#WirelessEarbuds", "#NoiseCancelling", "#TechGadgets", "#Deals"],
        "keywords": ["wireless earbuds", "noise cancelling", "bluetooth earbuds", "ANC"],
        "rag_sources": MARKET_STRATEGY["rag_sources"],
        "compliance": {"passed": True},
    }


def _make_copies() -> list[dict]:
    return [
        _make_copy(i + 1, style, headline)
        for i, (style, headline) in enumerate(COPY_VARIANTS)
    ]


# 合规审查结果
COMPLIANCE_PASS = {"passed": True, "violations": [], "feedback": None}

# 卖点提炼（3~5 条核心卖点，含事实依据与优先级）
SELL_POINTS = {
    "product_type": "无线入耳式降噪耳机",
    "audience": "18-35 岁通勤族与学生党",
    "scenario": "日常通勤中的安静聆听",
    "core_points": [
        {
            "point": "主动降噪隔绝环境噪音",
            "type": "功能",
            "evidence": "产品识别结果：Active Noise Cancellation",
            "priority": 1,
        },
        {
            "point": "30 小时总续航",
            "type": "功能",
            "evidence": "产品识别结果：30-hour total battery life",
            "priority": 2,
        },
        {
            "point": "IPX5 防水，运动可用",
            "type": "材质",
            "evidence": "产品识别结果：IPX5 water resistance",
            "priority": 3,
        },
        {
            "point": "蓝牙 5.3 低延迟连接",
            "type": "功能",
            "evidence": "产品识别结果：Bluetooth 5.3 low latency",
            "priority": 4,
        },
    ],
}

# 广告策划（主题 + 构图/版式 + 多版本文案）
AD_PLAN = {
    "ad_theme": "把噪音关在门外，把专注留给自己",
    "layout": {
        "composition": "center_hero",
        "text_area": "top",
        "subject_position": "center-lower",
        "visual_focus": "产品居中为绝对焦点，光带引导视线，文字位于左上留白区",
    },
    "versions": None,  # 运行时由 _make_copies() 填充
}

# 视觉设计（提示词生成）
PROMPT_GEN = {
    "image_prompt": "Modern tech-product advertising background: deep charcoal acoustic-texture wall, cool rim light with subtle brand-color light streaks, cinematic side lighting, shallow depth of field, background softly blurred, large empty clean space in the center of the frame for product placement, premium commercial advertising photography, sharp details, high dynamic range, 8k, ultra detailed",
    "negative_prompt": "product, people, person, human, face, hand, animal, text, letters, watermark, logo, furniture, window, curtains, plant, extra objects, duplicate, distorted, deformed, melted, low quality, blurry, noise, cartoon, illustration, fake text",
    "composition": "hero",
    "palette": ["#0F172A", "#14B8A6", "#E2E8F0"],
    "text_area": "top",
    "template_id": "hero",
    "product_placement": {"position": "center-lower", "scale": 0.72},
}


MOCK_RESPONSES: dict[str, dict] = {
    "product_analysis": PRODUCT_ANALYSIS,
    "market_strategy": MARKET_STRATEGY,
    "sell_points": SELL_POINTS,
    "ad_plan": {"ad_theme": AD_PLAN["ad_theme"], "layout": AD_PLAN["layout"], "versions": _make_copies()},
    "compliance_check": COMPLIANCE_PASS,
    "prompt_gen": PROMPT_GEN,
    "translate": {"translated": "Translated text (mock)"},
}


def mock_response(purpose: str) -> str:
    """返回指定 purpose 的 mock JSON 字符串。"""
    data = MOCK_RESPONSES.get(purpose, {"result": "mock"})
    return json.dumps(data, ensure_ascii=False)
