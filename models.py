from pydantic import BaseModel


class Container(BaseModel):
    name: str
    price: float
    is_offer: bool | None = None


class Result(BaseModel):
    success: bool
    message: str
