"""ORM 模型：产品、知识库、广告任务、文案、图片、用量。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Product(Base, TimestampMixin):
    """产品识别结果（可编辑）。"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(128), default="")
    material: Mapped[str | None] = mapped_column(String(128), nullable=True)
    color: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shape: Mapped[str | None] = mapped_column(String(128), nullable=True)
    scenes: Mapped[list[str]] = mapped_column(JSON, default=list)
    selling_points: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_audience: Mapped[str] = mapped_column(String(255), default="")
    brand_suspected: Mapped[str | None] = mapped_column(String(128), nullable=True)
    risk_flags: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    subject_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 价格/促销等

    ad_tasks: Mapped[list["AdTask"]] = relationship(back_populates="product")


class KbDocument(Base, TimestampMixin):
    """知识库文档。"""

    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    source_type: Mapped[str] = mapped_column(String(32), default="")
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/processing/done/failed
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    chunks: Mapped[list["KbChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KbChunk(Base):
    """知识库切分块。"""

    __tablename__ = "kb_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text)
    embedding_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    document: Mapped["KbDocument"] = relationship(back_populates="chunks")


class AdTask(Base, TimestampMixin):
    """广告生成任务。"""

    __tablename__ = "ad_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), nullable=True, index=True
    )
    country: Mapped[str] = mapped_column(String(64), default="US")
    language: Mapped[str] = mapped_column(String(16), default="en")
    platform: Mapped[str] = mapped_column(String(64), default="Amazon")
    style: Mapped[str] = mapped_column(String(32), default="promo")
    price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    promotion: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_preset: Mapped[str] = mapped_column(String(32), default="1:1")
    # 文案版本数（由请求透传，1~3）
    num_versions: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/running/success/failed/cancelled
    current_node: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    # 取消请求标记：落库以便多 worker / 重启后仍能中断任务
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # 用户在产品卡片上手工修正的产品字段覆盖（name/category/material/color/selling_points），
    # 生成时叠加在识别结果之上；为空表示完全采用识别结果
    product_override: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 广告策划 Agent 确定的广告主题（结果页展示，展示本次创意的统领概念）
    ad_theme: Mapped[str] = mapped_column(String(255), default="", server_default="")

    product: Mapped["Product | None"] = relationship(back_populates="ad_tasks")
    copy_versions: Mapped[list["AdCopyVersion"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    images: Mapped[list["AdImage"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class AdCopyVersion(Base, TimestampMixin):
    """单版广告文案。"""

    __tablename__ = "ad_copy_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("ad_tasks.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, default=0)
    style_variant: Mapped[str] = mapped_column(String(32), default="")
    headline: Mapped[str] = mapped_column(String(512), default="")
    subheadline: Mapped[str] = mapped_column(String(1024), default="")
    bullets: Mapped[list[str]] = mapped_column(JSON, default=list)
    cta: Mapped[str] = mapped_column(String(255), default="")
    platform_adaptations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    hashtags: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    rag_sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    compliance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    task: Mapped["AdTask"] = relationship(back_populates="copy_versions")


class AdImage(Base, TimestampMixin):
    """生成的广告图。"""

    __tablename__ = "ad_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("ad_tasks.id", ondelete="CASCADE"), index=True
    )
    copy_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str] = mapped_column(String(512), default="")
    template_id: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[str] = mapped_column(String(32), default="")
    scheme: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    task: Mapped["AdTask"] = relationship(back_populates="images")


class UsageLog(Base):
    """LLM / 图像模型调用成本与耗时记录。"""

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    purpose: Mapped[str] = mapped_column(String(64), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
