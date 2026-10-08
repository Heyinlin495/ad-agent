"""广告生成相关 Schema。"""
from typing import Any

from pydantic import BaseModel, Field


class ProductOverride(BaseModel):
    """用户在识别结果卡片上手工修正的产品字段（均可选，未填表示沿用识别结果）。"""

    name: str | None = None
    category: str | None = None
    material: str | None = None
    color: str | None = None
    selling_points: list[str] | None = None


class GenerateRequest(BaseModel):
    """创建广告生成任务的请求参数。"""

    product_id: int
    # 产品卡片上的编辑结果：叠加在识别结果之上，避免"可编辑但改了不生效"
    product_override: ProductOverride | None = None
    country: str = "US"
    language: str = "en"
    platform: str = "Amazon"  # Amazon/Meta/TikTok/Google/独立站
    style: str = "promo"  # promo/premium/humor/emotion/minimal
    price: str | None = None
    promotion: str | None = None
    size_preset: str = "1:1"  # 1:1/4:5/9:16/16:9/amazon
    num_versions: int = Field(default=3, ge=1, le=3)


class MarketStrategy(BaseModel):
    """市场策略 Agent 输出。"""

    point_priority: list[str] = Field(default_factory=list)  # 卖点排序
    emotional_tone: str = ""
    visual_style: str = ""
    cultural_notes: list[str] = Field(default_factory=list)
    compliance_rules: list[str] = Field(default_factory=list)
    rag_sources: list[dict[str, Any]] = Field(default_factory=list)


class Violation(BaseModel):
    kind: str = ""  # banned_word/exaggeration/infringement
    text: str = ""
    suggestion: str = ""


class ComplianceReport(BaseModel):
    passed: bool = True
    violations: list[Violation] = Field(default_factory=list)
    feedback: str | None = None


class AdCopy(BaseModel):
    """单版广告文案。"""

    version_no: int = 0
    style_variant: str = ""
    headline: str = ""
    subheadline: str = ""
    bullets: list[str] = Field(default_factory=list)
    cta: str = ""
    platform_adaptations: dict[str, Any] = Field(default_factory=dict)
    hashtags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    rag_sources: list[dict[str, Any]] = Field(default_factory=list)
    compliance: dict[str, Any] = Field(default_factory=dict)


class AdCopySet(BaseModel):
    versions: list[AdCopy] = Field(default_factory=list)


class VisualDesign(BaseModel):
    """视觉设计方案。"""

    image_prompt: str = ""
    composition: str = "centered"  # 构图
    palette: list[str] = Field(default_factory=list)
    text_area: str = ""  # 文字区域
    template_id: str = "centered"


class AdImageResult(BaseModel):
    image_url: str = ""
    template_id: str = ""
    size: str = ""
    scheme: dict[str, Any] = Field(default_factory=dict)


class TaskStatus(BaseModel):
    task_id: int
    status: str
    progress: int = 0
    current_node: str = ""
    error: str | None = None
    copies: list[AdCopy] = Field(default_factory=list)
    images: list[AdImageResult] = Field(default_factory=list)


class RegenerateRequest(BaseModel):
    """局部重生成。"""

    mode: str = "copy"  # copy/image
    copy_version_id: int | None = None
    edited_copy: AdCopy | None = None
    # 视觉重生成时的用户额外要求（如背景风格/色调/场景等，注入 prompt_gen 提示词）
    visual_hint: str | None = ""
