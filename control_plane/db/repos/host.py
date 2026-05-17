from __future__ import annotations

from db.models.host import Host
from db.repos.generic import GenericRepository
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


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

    async def delete_by_host_id(self, host_id: int) -> bool:
        return await self.delete_by_id(host_id)
