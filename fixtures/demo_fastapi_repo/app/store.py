"""Deterministic in-memory catalog store seeded with 25 items."""

from app.schemas import Item

_CATEGORIES = ("books", "hardware", "software", "accessories", "audio")


def _build_seed_items() -> tuple[Item, ...]:
    items: list[Item] = []
    for idx in range(1, 26):
        items.append(
            Item(
                id=f"item-{idx:03d}",
                name=f"Catalog Item {idx:03d}",
                category=_CATEGORIES[(idx - 1) % len(_CATEGORIES)],
                price_cents=500 + idx * 100,
            )
        )
    return tuple(items)


_CATALOG_ITEMS: tuple[Item, ...] = _build_seed_items()


def list_all_items() -> list[Item]:
    """Return all catalog items in deterministic ID order."""
    return list(_CATALOG_ITEMS)


def get_item_by_id(item_id: str) -> Item | None:
    """Return a matching catalog item or None if it does not exist."""
    for item in _CATALOG_ITEMS:
        if item.id == item_id:
            return item
    return None