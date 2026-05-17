from __future__ import annotations

from db.models.project import Project
from db.repos.generic import GenericRepository
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


class ProjectRepository(GenericRepository[Project]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Project)

    async def get_by_name(self, lab_id: int, project_name: str) -> Project | None:
        result = await self.session.execute(
            select(Project).where(
                Project.lab_id == lab_id,
                Project.project_name == project_name,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_lab_id(self, lab_id: int) -> list[Project]:
        result = await self.session.execute(
            select(Project).where(Project.lab_id == lab_id)
        )
        return list(result.scalars().all())

    async def delete_by_lab_id(self, lab_id: int) -> None:
        await self.session.execute(delete(Project).where(Project.lab_id == lab_id))
        await self.session.commit()

    async def delete_row(self, project_row: Project) -> None:
        await self.session.execute(delete(Project).where(Project.id == project_row.id))
        await self.session.commit()
