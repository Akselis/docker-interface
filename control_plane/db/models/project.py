from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, ForeignKey, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Boolean, DateTime, Float, Integer

from .base import Base


class ComposeSourceType(str, Enum):
    GIT = "git"
    ARCHIVE = "archive"
    INLINE = "inline"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint(
            "lab_id", "project_name", name="uq_projects_lab_id_project_name"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lab_id: Mapped[int] = mapped_column(
        ForeignKey("labs.id", ondelete="CASCADE"),
        nullable=False,
        unique=False,
        index=True,
    )

    project_name: Mapped[str] = mapped_column(nullable=False)
    source_type: Mapped[ComposeSourceType] = mapped_column(
        SAEnum(ComposeSourceType, name="compose_source_type_enum"),
        nullable=False,
    )

    source_url: Mapped[str | None] = mapped_column(nullable=True)
    ref: Mapped[str | None] = mapped_column(nullable=True)
    compose_file: Mapped[str] = mapped_column(
        nullable=False, default="docker-compose.yml"
    )
    compose_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    env: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)

    pull: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    build: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cpu_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory_limit_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.utcnow(),
        onupdate=lambda: datetime.utcnow(),
    )
