from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base


class ContainerStatus(str, Enum):
    CREATED = "created"
    STARTED = "started"
    STOPPED = "stopped"
    PAUSED = "paused"
    FAILED = "failed"


class Container(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    container_id: Mapped[str] = mapped_column(nullable=False, unique=True)
    image: Mapped[str] = mapped_column(nullable=False)
    lab_id: Mapped[int] = mapped_column(
        ForeignKey("labs.id", ondelete="CASCADE"),
        nullable=False,
        unique=False,
        index=True,
    )
    status: Mapped[ContainerStatus] = mapped_column(
        SAEnum(ContainerStatus, name="container_status_enum"),
        nullable=False,
        default=ContainerStatus.CREATED,
    )
    cpu_limit: Mapped[int] = mapped_column(nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_seen_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
