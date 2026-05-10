from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base


class LabStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class Lab(Base):
    __tablename__ = "labs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
        unique=False,
        index=True,
    )
    status: Mapped[LabStatus] = mapped_column(
        SAEnum(LabStatus, name="lab_status_enum"),
        nullable=False,
        default=LabStatus.STOPPED,
    )
    cpu_total: Mapped[int] = mapped_column(nullable=False)
    memory_total_mb: Mapped[int] = mapped_column(nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
