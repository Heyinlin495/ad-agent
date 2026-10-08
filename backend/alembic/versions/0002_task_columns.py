"""ad_tasks 增加 cancel_requested / num_versions。

- cancel_requested：取消标记落库，多 worker / 重启后仍可中断任务。
- num_versions：文案版本数由请求透传（原实现硬编码为 3）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_task_columns"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ad_tasks",
        sa.Column(
            "cancel_requested",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "ad_tasks",
        sa.Column(
            "num_versions",
            sa.Integer,
            nullable=False,
            server_default="3",
        ),
    )


def downgrade() -> None:
    op.drop_column("ad_tasks", "num_versions")
    op.drop_column("ad_tasks", "cancel_requested")
