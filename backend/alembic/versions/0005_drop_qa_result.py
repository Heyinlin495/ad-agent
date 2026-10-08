"""质量检查（QA）功能下线：删除 ad_images.qa_result 列

Revision ID: 0005_drop_qa_result
Revises: 0004_ad_theme
Create Date: 2026-10-07

质量检查节点（qa_check/refine）已从流水线整体移除，历史任务不再读写
qa_result；SQLite 删列走 batch 模式（重建表拷贝数据）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_drop_qa_result"
down_revision = "0004_ad_theme"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ad_images") as batch:
        batch.drop_column("qa_result")


def downgrade() -> None:
    with op.batch_alter_table("ad_images") as batch:
        batch.add_column(
            sa.Column("qa_result", sa.JSON(), nullable=False, server_default="{}")
        )
