"""add_default_internal_external_network_refs_to_labs

Revision ID: e1a2f38c4b77
Revises: c2f4c8e5a0f1
Create Date: 2026-05-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a2f38c4b77"
down_revision: Union[str, None] = "c2f4c8e5a0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "labs",
        sa.Column("default_internal_network_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "labs",
        sa.Column("default_external_network_id", sa.Integer(), nullable=True),
    )

    op.create_index(
        op.f("ix_labs_default_internal_network_id"),
        "labs",
        ["default_internal_network_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_labs_default_external_network_id"),
        "labs",
        ["default_external_network_id"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_labs_default_internal_network_id_networks",
        "labs",
        "networks",
        ["default_internal_network_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_labs_default_external_network_id_networks",
        "labs",
        "networks",
        ["default_external_network_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_labs_default_external_network_id_networks", "labs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_labs_default_internal_network_id_networks", "labs", type_="foreignkey"
    )

    op.drop_index(op.f("ix_labs_default_external_network_id"), table_name="labs")
    op.drop_index(op.f("ix_labs_default_internal_network_id"), table_name="labs")

    op.drop_column("labs", "default_external_network_id")
    op.drop_column("labs", "default_internal_network_id")
