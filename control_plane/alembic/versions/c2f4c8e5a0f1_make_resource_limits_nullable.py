"""make_resource_limits_nullable

Revision ID: c2f4c8e5a0f1
Revises: b1d2a67c2e94
Create Date: 2026-05-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2f4c8e5a0f1"
down_revision: Union[str, None] = "b1d2a67c2e94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("labs", "cpu_limit", existing_type=sa.Integer(), nullable=True)
    op.alter_column(
        "labs", "memory_limit_mb", existing_type=sa.Integer(), nullable=True
    )

    op.alter_column(
        "containers", "cpu_limit", existing_type=sa.Integer(), nullable=True
    )
    op.alter_column(
        "containers", "memory_limit_mb", existing_type=sa.Integer(), nullable=True
    )

    op.alter_column(
        "projects",
        "cpu_limit",
        existing_type=sa.Float(),
        nullable=True,
    )
    op.alter_column(
        "projects",
        "memory_limit_mb",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(sa.text("UPDATE labs SET cpu_limit = 0 WHERE cpu_limit IS NULL"))
    op.execute(
        sa.text("UPDATE labs SET memory_limit_mb = 0 WHERE memory_limit_mb IS NULL")
    )
    op.execute(sa.text("UPDATE containers SET cpu_limit = 0 WHERE cpu_limit IS NULL"))
    op.execute(
        sa.text(
            "UPDATE containers SET memory_limit_mb = 0 WHERE memory_limit_mb IS NULL"
        )
    )
    op.execute(sa.text("UPDATE projects SET cpu_limit = 0 WHERE cpu_limit IS NULL"))
    op.execute(
        sa.text("UPDATE projects SET memory_limit_mb = 0 WHERE memory_limit_mb IS NULL")
    )

    op.alter_column("labs", "cpu_limit", existing_type=sa.Integer(), nullable=False)
    op.alter_column(
        "labs", "memory_limit_mb", existing_type=sa.Integer(), nullable=False
    )

    op.alter_column(
        "containers", "cpu_limit", existing_type=sa.Integer(), nullable=False
    )
    op.alter_column(
        "containers", "memory_limit_mb", existing_type=sa.Integer(), nullable=False
    )

    op.alter_column(
        "projects",
        "cpu_limit",
        existing_type=sa.Float(),
        nullable=False,
    )
    op.alter_column(
        "projects",
        "memory_limit_mb",
        existing_type=sa.Integer(),
        nullable=False,
    )
