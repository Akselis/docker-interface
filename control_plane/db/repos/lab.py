from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.lab import Lab
from db.repos.generic import GenericRepository


class LabRepository(GenericRepository[Lab]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Lab)

    async def get_by_name(self, lab_name: str) -> Lab | None:
        result = await self.session.execute(select(Lab).where(Lab.name == lab_name))
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Lab]:
        result = await self.session.execute(select(Lab).order_by(Lab.id.asc()))
        return list(result.scalars().all())

    async def list_by_host_id(self, host_id: int) -> list[Lab]:
        result = await self.session.execute(select(Lab).where(Lab.host_id == host_id))
        return list(result.scalars().all())

    async def delete_by_lab_id(self, lab_id: int) -> None:
        await self.session.execute(delete(Lab).where(Lab.id == lab_id))
        await self.session.commit()
