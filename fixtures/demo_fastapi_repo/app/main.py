"""FastAPI application exposing catalog endpoints."""

from fastapi import FastAPI, HTTPException, status

from app.schemas import Item, ItemListResponse
from app.store import get_item_by_id, list_all_items

app = FastAPI(title="Demo Catalog API", version="0.1.0")


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return liveness status."""
    return {"status": "ok"}


@app.get("/items", response_model=ItemListResponse)
def list_items() -> ItemListResponse:
    """Return all catalog items."""
    items = list_all_items()
    return ItemListResponse(items=items, total=len(items))


@app.get("/items/{item_id}", response_model=Item)
def get_item(item_id: str) -> Item:
    """Retrieve a single catalog item by its identifier."""
    item = get_item_by_id(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )
    return item