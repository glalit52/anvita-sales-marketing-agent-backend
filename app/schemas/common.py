from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Schema(BaseModel):
    """API models speak camelCase JSON (matching the web app) and accept either case on input."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class Page[T](Schema):
    items: list[T]
    total: int
    limit: int
    offset: int


class Ok(Schema):
    ok: bool = True
