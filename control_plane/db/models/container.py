from datetime import datetime
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base
from .shared import LifetimeType


class ContainerStatus(str, Enum):
    CREATED = "created"
    STARTED = "started"
    STOPPED = "stopped"
    PAUSED = "paused"


class Container(Base):
    __tablename__ = "containers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    container_id: Mapped[str] = mapped_column(nullable=False, unique=True)
    name: Mapped[str] = mapped_column(nullable=False)
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
    cpu_limit: Mapped[int | None] = mapped_column(nullable=True)
    memory_limit_mb: Mapped[int | None] = mapped_column(nullable=True)
    lifetime_type: Mapped[LifetimeType] = mapped_column(
        SAEnum(LifetimeType, name="lifetime_type_enum"),
        nullable=False,
        default=LifetimeType.EPHEMERAL,
    )
    time_to_live_seconds: Mapped[int | None] = mapped_column(nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )
    last_seen_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )
