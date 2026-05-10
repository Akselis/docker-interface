from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base


class HostStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DRAINING = "draining"


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    hostname: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    ip_address: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[HostStatus] = mapped_column(
        SAEnum(HostStatus, name="host_status_enum"),
        nullable=False,
        default=HostStatus.OFFLINE,
    )
    cpu_total: Mapped[int] = mapped_column(nullable=False)
    memory_total_mb: Mapped[int] = mapped_column(nullable=False)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_heartbeat_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
