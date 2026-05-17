from __future__ import annotations

from db.models.container import Container
from db.repos.generic import GenericRepository
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


class ContainerRepository(GenericRepository[Container]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Container)

    async def get_by_name(self, lab_id: int, container_name: str) -> Container | None:
        result = await self.session.execute(
            select(Container).where(
                Container.lab_id == lab_id,
                Container.name == container_name,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_container_id(self, container_id: str) -> Container | None:
        result = await self.session.execute(
            select(Container).where(Container.container_id == container_id)
        )
        return result.scalar_one_or_none()

    async def list_by_lab_id(self, lab_id: int) -> list[Container]:
        result = await self.session.execute(
            select(Container).where(Container.lab_id == lab_id)
        )
        return list(result.scalars().all())

    async def delete_by_lab_id(self, lab_id: int) -> None:
        await self.session.execute(delete(Container).where(Container.lab_id == lab_id))
        await self.session.commit()

    async def delete_row(self, container_row: Container) -> None:
        await self.session.execute(
            delete(Container).where(Container.id == container_row.id)
        )
        await self.session.commit()
