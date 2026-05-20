from __future__ import annotations

from db.models.network import Network
from db.repos.generic import GenericRepository
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


class NetworkRepository(GenericRepository[Network]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Network)

    async def get_by_name(self, lab_id: int, network_name: str) -> Network | None:
        result = await self.session.execute(
            select(Network).where(
                Network.lab_id == lab_id,
                Network.name == network_name,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_lab_id(self, lab_id: int) -> list[Network]:
        result = await self.session.execute(
            select(Network).where(Network.lab_id == lab_id)
        )
        return list(result.scalars().all())

    async def delete_by_lab_id(self, lab_id: int) -> None:
        await self.session.execute(delete(Network).where(Network.lab_id == lab_id))
        await self.session.commit()

    async def delete_row(self, network_row: Network) -> None:
        await self.session.execute(delete(Network).where(Network.id == network_row.id))
        await self.session.commit()
