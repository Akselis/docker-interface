"""align_labs_schema_with_model

Revision ID: 7e2d93b91f0f
Revises: d4c6f1cfe9a1
Create Date: 2026-05-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7e2d93b91f0f"
down_revision: Union[str, None] = "d4c6f1cfe9a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("labs", sa.Column("name", sa.String(length=120), nullable=True))
    op.add_column("labs", sa.Column("cpu_limit", sa.Integer(), nullable=True))
    op.add_column("labs", sa.Column("memory_limit_mb", sa.Integer(), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, cpu_total, memory_total_mb FROM labs")
    ).fetchall()

    for row in rows:
        lab_id = int(row[0])
        cpu_total = int(row[1]) if row[1] is not None else 0
        memory_total_mb = int(row[2]) if row[2] is not None else 0

        conn.execute(
            sa.text(
                """
                UPDATE labs
                SET name = :name,
                    cpu_limit = :cpu_limit,
                    memory_limit_mb = :memory_limit_mb
                WHERE id = :lab_id
                """
            ),
            {
                "name": f"lab-{lab_id}",
                "cpu_limit": cpu_total,
                "memory_limit_mb": memory_total_mb,
                "lab_id": lab_id,
            },
        )

    op.alter_column("labs", "name", existing_type=sa.String(length=120), nullable=False)
    op.alter_column("labs", "cpu_limit", existing_type=sa.Integer(), nullable=False)
    op.alter_column(
        "labs", "memory_limit_mb", existing_type=sa.Integer(), nullable=False
    )

    op.create_index(op.f("ix_labs_name"), "labs", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_labs_name"), table_name="labs")
    op.drop_column("labs", "memory_limit_mb")
    op.drop_column("labs", "cpu_limit")
    op.drop_column("labs", "name")
