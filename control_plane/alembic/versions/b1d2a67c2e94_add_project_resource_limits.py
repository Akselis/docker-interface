"""add_project_resource_limits

Revision ID: b1d2a67c2e94
Revises: 39b02a7bb6b0
Create Date: 2026-05-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1d2a67c2e94"
down_revision: Union[str, None] = "39b02a7bb6b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("cpu_limit", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "projects",
        sa.Column("memory_limit_mb", sa.Integer(), nullable=False, server_default="0"),
    )

    op.alter_column(
        "projects",
        "cpu_limit",
        existing_type=sa.Float(),
        server_default=None,
        existing_nullable=False,
    )
    op.alter_column(
        "projects",
        "memory_limit_mb",
        existing_type=sa.Integer(),
        server_default=None,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.drop_column("projects", "memory_limit_mb")
    op.drop_column("projects", "cpu_limit")
