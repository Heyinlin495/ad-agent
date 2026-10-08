"""ad_tasks 增加 product_override。

产品信息卡片标注「可编辑」，但此前编辑后的名称/品类/材质/颜色/卖点并未随
生成请求透传（GenerateRequest 无对应字段），后端始终使用识别原始结果。
新增 product_override(JSON) 落库保存用户在卡片上的手工修正，生成时叠加。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_task_product_override"
down_revision: Union[str, None] = "0002_task_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ad_tasks",
        sa.Column("product_override", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ad_tasks", "product_override")
