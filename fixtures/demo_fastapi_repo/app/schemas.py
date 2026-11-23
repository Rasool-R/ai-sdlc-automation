"""Pydantic response schemas for the catalog API."""

from pydantic import BaseModel, Field


class Item(BaseModel):
    """Single catalog item."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    price_cents: int = Field(ge=0)


class ItemListResponse(BaseModel):
    """Collection response returned by GET /items."""

    items: list[Item]
    total: int = Field(ge=0)