from __future__ import annotations

from typing import Generic, TypeVar

from db.models.base import Base
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT", bound=Base)


class GenericRepository(Generic[ModelT]):
    def __init__(self, session: AsyncSession, model_type: type[ModelT]):
        self.session = session
        self.model_type = model_type

    async def get_by_id(self, entity_id: object) -> ModelT | None:
        return await self.session.get(self.model_type, entity_id)

    async def insert(self, model: ModelT) -> ModelT:
        self.session.add(model)
        await self.session.flush()
        await self.session.refresh(model)
        await self.session.commit()
        return model

    async def update(self, model: ModelT) -> ModelT:
        merged = await self.session.merge(model)
        await self.session.flush()
        await self.session.refresh(merged)
        await self.session.commit()
        return merged

    async def delete_by_id(self, entity_id: object) -> bool:
        entity = await self.get_by_id(entity_id)
        if entity is None:
            return False

        await self.session.delete(entity)
        await self.session.commit()
        return True
