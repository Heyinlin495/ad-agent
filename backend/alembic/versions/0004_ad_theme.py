"""广告主题（ad_theme）

Revision ID: 0004_ad_theme
Revises: 0003_task_product_override
Create Date: 2026-10-07

广告策划 Agent 输出的广告主题需要展示在结果页，故落库为 ad_tasks.ad_theme。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_ad_theme"
down_revision = "0003_task_product_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ad_tasks",
        sa.Column("ad_theme", sa.String(length=255), nullable=True, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("ad_tasks", "ad_theme")
