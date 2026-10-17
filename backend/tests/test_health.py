"""Tests for the health check endpoint (Phase 1)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_200_and_ok_status() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_response_matches_declared_contract() -> None:
    response = client.get("/health")

    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == {"status"}
    assert body["status"] == "ok"
