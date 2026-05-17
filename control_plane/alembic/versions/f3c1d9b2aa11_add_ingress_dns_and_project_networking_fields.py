"""add_ingress_dns_and_project_networking_fields

Revision ID: f3c1d9b2aa11
Revises: e1a2f38c4b77
Create Date: 2026-05-17 00:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3c1d9b2aa11"
down_revision: Union[str, None] = "e1a2f38c4b77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


project_network_mode_enum = sa.Enum(
    "INTERNAL_PRIVATE",
    "INTERNAL_EXPOSED",
    "EXTERNAL_PRIVATE",
    "EXTERNAL_EXPOSED",
    name="project_network_mode_enum",
)


def upgrade() -> None:
    op.add_column(
        "hosts", sa.Column("base_domain", sa.String(length=255), nullable=True)
    )
    op.add_column("hosts", sa.Column("dns_zone", sa.String(length=255), nullable=True))
    op.add_column(
        "hosts", sa.Column("ingress_target", sa.String(length=255), nullable=True)
    )

    project_network_mode_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "projects",
        sa.Column(
            "network_mode",
            project_network_mode_enum,
            nullable=False,
            server_default="INTERNAL_PRIVATE",
        ),
    )
    op.add_column("projects", sa.Column("exposed_services", sa.JSON(), nullable=True))

    op.alter_column(
        "projects",
        "network_mode",
        existing_type=project_network_mode_enum,
        server_default=None,
        existing_nullable=False,
    )


def downgrade() -> None:
    op.drop_column("projects", "exposed_services")
    op.drop_column("projects", "network_mode")
    project_network_mode_enum.drop(op.get_bind(), checkfirst=True)

    op.drop_column("hosts", "ingress_target")
    op.drop_column("hosts", "dns_zone")
    op.drop_column("hosts", "base_domain")
