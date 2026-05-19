from datetime import datetime
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
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
    name: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True, index=True
    )
    host_id: Mapped[int] = mapped_column(
        ForeignKey("hosts.id", ondelete="CASCADE"),
        nullable=False,
        unique=False,
        index=True,
    )
    status: Mapped[LabStatus] = mapped_column(
        SAEnum(LabStatus, name="lab_status_enum"),
        nullable=False,
        default=LabStatus.RUNNING,
    )
    default_internal_network_id: Mapped[int | None] = mapped_column(
        ForeignKey("networks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    default_external_network_id: Mapped[int | None] = mapped_column(
        ForeignKey("networks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    cpu_limit: Mapped[int | None] = mapped_column(nullable=True)
    memory_limit_mb: Mapped[int | None] = mapped_column(nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )
