"""add_projects_and_host_connection_fields

Revision ID: d4c6f1cfe9a1
Revises: 838f50d269a9
Create Date: 2026-05-14 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4c6f1cfe9a1"
down_revision: Union[str, None] = "838f50d269a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column("port", sa.Integer(), nullable=False, server_default="8000"),
    )
    op.add_column(
        "hosts",
        sa.Column(
            "scheme",
            sa.String(length=10),
            nullable=False,
            server_default="http",
        ),
    )
    op.add_column(
        "hosts",
        sa.Column("api_key_secret_path", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_hosts_api_key_secret_path",
        "hosts",
        ["api_key_secret_path"],
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("lab_id", sa.Integer(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum("GIT", "ARCHIVE", "INLINE", name="compose_source_type_enum"),
            nullable=False,
        ),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("ref", sa.String(), nullable=True),
        sa.Column(
            "compose_file",
            sa.String(),
            nullable=False,
            server_default="docker-compose.yml",
        ),
        sa.Column("compose_content", sa.Text(), nullable=True),
        sa.Column("env", sa.JSON(), nullable=True),
        sa.Column("pull", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("build", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lab_id"], ["labs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lab_id",
            "project_name",
            name="uq_projects_lab_id_project_name",
        ),
    )
    op.create_index(op.f("ix_projects_lab_id"), "projects", ["lab_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_projects_lab_id"), table_name="projects")
    op.drop_table("projects")

    op.drop_constraint("uq_hosts_api_key_secret_path", "hosts", type_="unique")
    op.drop_column("hosts", "api_key_secret_path")
    op.drop_column("hosts", "scheme")
    op.drop_column("hosts", "port")
