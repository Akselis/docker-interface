from pydantic import BaseModel


class ContainerArgs(BaseModel):
    name: str
    image: str
    is_offer: bool | None = None


class Result(BaseModel):
    success: bool
    message: str
