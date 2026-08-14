import importlib
import pytest
from fastapi.testclient import TestClient


def load_app(monkeypatch):
    monkeypatch.delenv("GENROUTER_API_TOKEN", raising=False)
    monkeypatch.setenv("GENROUTER_ALLOWED_ORIGINS", "http://router.local")
    import app.main

    return importlib.reload(app.main).app


def test_api_works_without_token_when_env_not_set(monkeypatch):
    app = load_app(monkeypatch)
    client = TestClient(app)

    response = client.get("/api/proxies")
    assert response.status_code == 200


def test_api_requires_bearer_token_when_env_set(monkeypatch):
    monkeypatch.setenv("GENROUTER_API_TOKEN", "my-secret-token")
    import app.main
    app = importlib.reload(app.main).app
    client = TestClient(app)

    response_no_token = client.get("/api/proxies")
    assert response_no_token.status_code == 401

    response_valid_token = client.get(
        "/api/proxies",
        headers={"Authorization": "Bearer my-secret-token"},
    )
    assert response_valid_token.status_code == 200
