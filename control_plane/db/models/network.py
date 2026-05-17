from datetime import datetime
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from .base import Base


class NetworkDriver(str, Enum):
    BRIDGE = "bridge"
    OVERLAY = "overlay"


class Network(Base):
    __tablename__ = "networks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    network_id: Mapped[str] = mapped_column(nullable=False, unique=True)
    name: Mapped[str] = mapped_column(nullable=False)
    lab_id: Mapped[int] = mapped_column(
        ForeignKey("labs.id", ondelete="CASCADE"),
        nullable=False,
        unique=False,
        index=True,
    )
    driver: Mapped[NetworkDriver] = mapped_column(
        SAEnum(NetworkDriver, name="network_driver_enum"),
        nullable=False,
        default=NetworkDriver.BRIDGE,
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )
