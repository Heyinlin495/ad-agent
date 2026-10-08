"""产品相关 Schema。"""
from typing import Any

from pydantic import BaseModel, Field


class RiskFlag(BaseModel):
    """风险标记。"""

    level: str = "info"  # info/warning/danger
    kind: str = ""  # infringement/sensitive_category/logo
    description: str = ""


class ProductAnalysis(BaseModel):
    """多模态识别输出的结构化结果。"""

    category: str = ""
    name: str = ""
    material: str | None = None
    color: str | None = None
    shape: str | None = None
    scenes: list[str] = Field(default_factory=list)
    selling_points: list[str] = Field(default_factory=list)  # 3-5 条
    target_audience: str = ""
    brand_suspected: str | None = None
    risk_flags: list[RiskFlag] = Field(default_factory=list)


class ProductEdit(BaseModel):
    """用户编辑补充字段（价格/促销/品牌等）。"""

    price: str | None = None
    promotion: str | None = None
    brand_name: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ProductOut(ProductAnalysis):
    id: int
    subject_image_url: str | None = None
    original_image_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    """产品识别请求（上传图片由 multipart 承载，这里放可选覆盖项）。"""

    language: str = "zh"
    hint: str | None = None  # 用户补充描述


class QualityReport(BaseModel):
    """图片质量检测结果。"""

    ok: bool = True
    blurry: bool = False
    too_dark: bool = False
    subject_too_small: bool = False
    messages: list[str] = Field(default_factory=list)


class AnalyzeResponse(BaseModel):
    """产品识别接口响应。"""

    product: ProductOut
    quality: QualityReport
