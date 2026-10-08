"""初始迁移：核心业务表。

M0 阶段仅建表骨架，后续阶段如需改字段请新增迁移。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("category", sa.String(128), nullable=False, server_default=""),
        sa.Column("material", sa.String(128), nullable=True),
        sa.Column("color", sa.String(128), nullable=True),
        sa.Column("shape", sa.String(128), nullable=True),
        sa.Column("scenes", sa.JSON, nullable=False),
        sa.Column("selling_points", sa.JSON, nullable=False),
        sa.Column("target_audience", sa.String(255), nullable=False, server_default=""),
        sa.Column("brand_suspected", sa.String(128), nullable=True),
        sa.Column("risk_flags", sa.JSON, nullable=False),
        sa.Column("subject_image_url", sa.String(512), nullable=True),
        sa.Column("original_image_url", sa.String(512), nullable=True),
        sa.Column("extra", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("source_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("platform", sa.String(64), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("file_url", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding_id", sa.String(255), nullable=True),
        sa.Column("meta", sa.JSON, nullable=False),
        sa.Column("order_index", sa.Integer, nullable=False, server_default="0"),
    )

    op.create_table(
        "ad_tasks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=True, index=True),
        sa.Column("country", sa.String(64), nullable=False, server_default="US"),
        sa.Column("language", sa.String(16), nullable=False, server_default="en"),
        sa.Column("platform", sa.String(64), nullable=False, server_default="Amazon"),
        sa.Column("style", sa.String(32), nullable=False, server_default="promo"),
        sa.Column("price", sa.String(64), nullable=True),
        sa.Column("promotion", sa.String(255), nullable=True),
        sa.Column("size_preset", sa.String(32), nullable=False, server_default="1:1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("current_node", sa.String(64), nullable=False, server_default=""),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("progress", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "ad_copy_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("ad_tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("version_no", sa.Integer, nullable=False, server_default="0"),
        sa.Column("style_variant", sa.String(32), nullable=False, server_default=""),
        sa.Column("headline", sa.String(512), nullable=False, server_default=""),
        sa.Column("subheadline", sa.String(1024), nullable=False, server_default=""),
        sa.Column("bullets", sa.JSON, nullable=False),
        sa.Column("cta", sa.String(255), nullable=False, server_default=""),
        sa.Column("platform_adaptations", sa.JSON, nullable=False),
        sa.Column("hashtags", sa.JSON, nullable=False),
        sa.Column("keywords", sa.JSON, nullable=False),
        sa.Column("rag_sources", sa.JSON, nullable=False),
        sa.Column("compliance", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "ad_images",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("ad_tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("copy_version_id", sa.Integer, nullable=True),
        sa.Column("image_url", sa.String(512), nullable=False, server_default=""),
        sa.Column("template_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("size", sa.String(32), nullable=False, server_default=""),
        sa.Column("scheme", sa.JSON, nullable=False),
        sa.Column("qa_result", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "usage_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, nullable=True, index=True),
        sa.Column("provider", sa.String(64), nullable=False, server_default=""),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("purpose", sa.String(64), nullable=False, server_default=""),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost", sa.Float, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("usage_logs")
    op.drop_table("ad_images")
    op.drop_table("ad_copy_versions")
    op.drop_table("ad_tasks")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
    op.drop_table("products")
