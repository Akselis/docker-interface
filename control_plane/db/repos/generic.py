from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Generic, TypeVar

from db.models.base import Base
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT", bound=Base)


class GenericRepository(Generic[ModelT]):
    def __init__(self, session: AsyncSession, model_type: type[ModelT]):
        self.session = session
        self.model_type = model_type

    async def get_by_id(self, entity_id: object) -> ModelT | None:
        return await self.session.get(self.model_type, entity_id)

    async def get_by_args(
        self,
        args: ModelT | Mapping[str, object],
        limit: int | None = None,
    ) -> list[ModelT]:
        filters = self._extract_filters(args)

        stmt = select(self.model_type)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model_type, key) == value)

        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def insert(self, model: ModelT) -> ModelT:
        self.session.add(model)
        await self.session.flush()
        await self.session.refresh(model)
        await self.session.commit()
        return model

    async def insert_many(self, models: Sequence[ModelT]) -> list[ModelT]:
        self.session.add_all(list(models))
        await self.session.flush()

        for model in models:
            await self.session.refresh(model)

        await self.session.commit()
        return list(models)

    async def update(self, model: ModelT) -> ModelT:
        merged = await self.session.merge(model)
        await self.session.flush()
        await self.session.refresh(merged)
        await self.session.commit()
        return merged

    async def update_many(self, models: Sequence[ModelT]) -> list[ModelT]:
        merged_models: list[ModelT] = []
        for model in models:
            merged_models.append(await self.session.merge(model))

        await self.session.flush()

        for model in merged_models:
            await self.session.refresh(model)

        await self.session.commit()
        return merged_models

    async def delete_by_id(self, entity_id: object) -> bool:
        entity = await self.get_by_id(entity_id)
        if entity is None:
            return False

        await self.session.delete(entity)
        await self.session.commit()
        return True

    def _extract_filters(
        self, args: ModelT | Mapping[str, object]
    ) -> dict[str, object]:
        allowed_columns = {column.key for column in inspect(self.model_type).columns}
        filters: dict[str, object] = {}

        if isinstance(args, Mapping):
            for key, value in args.items():
                if key in allowed_columns and value is not None:
                    filters[key] = value
            return filters

        for key in allowed_columns:
            value = getattr(args, key, None)
            if value is not None:
                filters[key] = value

        return filters
