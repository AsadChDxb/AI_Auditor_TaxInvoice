"""Tests for the browser (CORS) policy that lets the Next.js UI call the API (Phase 8).

The UI runs on a different origin (port 3000) than the API (port 8000), so the browser
sends a preflight request before ``POST /documents/upload``. These tests pin the default
allow-list, the ``VAT_AUDIT_CORS_ALLOW_ORIGINS`` override and the fact that an unlisted
origin is refused.
"""

from fastapi.testclient import TestClient

from app.config import DEFAULT_CORS_ALLOW_ORIGINS, get_cors_allow_origins

UI_ORIGIN = "http://localhost:3000"
ORIGIN_ENV = "VAT_AUDIT_CORS_ALLOW_ORIGINS"


def test_preflight_from_the_ui_origin_is_allowed(client: TestClient) -> None:
    response = client.options(
        "/documents/upload",
        headers={
            "Origin": UI_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == UI_ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]


def test_unlisted_origin_gets_no_allow_header(client: TestClient) -> None:
    response = client.options(
        "/documents/upload",
        headers={
            "Origin": "http://not-the-ui.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert "access-control-allow-origin" not in response.headers


def test_simple_request_reports_the_allowed_origin(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": UI_ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == UI_ORIGIN


def test_credentials_are_never_allowed(client: TestClient) -> None:
    """The API has no cookies or sessions, so credentials stay switched off."""

    response = client.get("/health", headers={"Origin": UI_ORIGIN})

    assert "access-control-allow-credentials" not in response.headers


def test_defaults_cover_the_local_nextjs_server(monkeypatch) -> None:
    monkeypatch.delenv(ORIGIN_ENV, raising=False)

    assert get_cors_allow_origins() == list(DEFAULT_CORS_ALLOW_ORIGINS)


def test_origins_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv(
        ORIGIN_ENV, "https://audit.example, http://localhost:5173"
    )

    assert get_cors_allow_origins() == [
        "https://audit.example",
        "http://localhost:5173",
    ]


def test_star_allows_every_origin(monkeypatch) -> None:
    monkeypatch.setenv(ORIGIN_ENV, "*")

    assert get_cors_allow_origins() == ["*"]


def test_blank_origin_setting_falls_back_to_the_defaults(monkeypatch) -> None:
    monkeypatch.setenv(ORIGIN_ENV, "   ")

    assert get_cors_allow_origins() == list(DEFAULT_CORS_ALLOW_ORIGINS)
