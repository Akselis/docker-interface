from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.host import Host, HostStatus
from db.repos.generic import GenericRepository


class HostRepository(GenericRepository[Host]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Host)

    async def get_by_hostname(self, hostname: str) -> Host | None:
        result = await self.session.execute(
            select(Host).where(Host.hostname == hostname)
        )
        return result.scalar_one_or_none()

    async def get_by_ip(self, ip_address: str) -> Host | None:
        result = await self.session.execute(
            select(Host).where(Host.ip_address == ip_address)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Host]:
        result = await self.session.execute(select(Host).order_by(Host.id.asc()))
        return list(result.scalars().all())

    async def resolve_from_heartbeat_payload(
        self, payload: dict[str, Any]
    ) -> Host | None:
        host_id_obj = payload.get("host_id")

        if isinstance(host_id_obj, int):
            return await self.get_by_id(host_id_obj)

        if isinstance(host_id_obj, str) and host_id_obj:
            by_hostname = await self.get_by_hostname(host_id_obj)
            if by_hostname is not None:
                return by_hostname

        ip_obj = payload.get("ip_address")
        if isinstance(ip_obj, str) and ip_obj:
            by_ip = await self.get_by_ip(ip_obj)
            if by_ip is not None:
                return by_ip

        return None

    async def mark_online(self, host: Host) -> Host:
        host.status = HostStatus.ONLINE
        host.last_heartbeat_utc = datetime.utcnow()
        return await self.update(host)

    async def delete_by_host_id(self, host_id: int) -> bool:
        return await self.delete_by_id(host_id)
