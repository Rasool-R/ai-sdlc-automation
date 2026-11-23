"""Baseline unit tests for the Demo Catalog API."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_items_returns_all_seeded_items() -> None:
    response = client.get("/items")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 25
    assert len(body["items"]) == 25
    assert body["items"][0]["id"] == "item-001"


def test_get_single_item_and_not_found() -> None:
    found = client.get("/items/item-005")
    assert found.status_code == 200
    assert found.json()["name"] == "Catalog Item 005"

    missing = client.get("/items/item-999")
    assert missing.status_code == 404