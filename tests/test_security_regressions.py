import importlib

import pytest
from fastapi.testclient import TestClient


def load_app(monkeypatch):
    monkeypatch.setenv("GENROUTER_API_TOKEN", "test-admin-token")
    monkeypatch.setenv("GENROUTER_ALLOWED_ORIGINS", "http://router.local")
    import app.main

    return importlib.reload(app.main).app


def test_pac_endpoint_returns_pac_file_without_name_error(monkeypatch):
    app = load_app(monkeypatch)
    client = TestClient(app)

    response = client.get("/proxy.pac")

    assert response.status_code == 200
    assert "FindProxyForURL" in response.text


def test_management_api_requires_bearer_token(monkeypatch):
    app = load_app(monkeypatch)
    client = TestClient(app)

    response = client.get("/api/proxies")

    assert response.status_code == 401


def test_management_api_accepts_configured_bearer_token(monkeypatch):
    app = load_app(monkeypatch)
    client = TestClient(app)

    response = client.get(
        "/api/proxies",
        headers={"Authorization": "Bearer test-admin-token"},
    )

    assert response.status_code == 200


def test_proxy_list_does_not_expose_password(monkeypatch):
    app = load_app(monkeypatch)
    client = TestClient(app)

    response = client.get(
        "/api/proxies",
        headers={"Authorization": "Bearer test-admin-token"},
    )

    assert response.status_code == 200
    for proxy in response.json()["proxies"]:
        assert "password" not in proxy
        assert "username" not in proxy


def test_settings_does_not_expose_hotspot_password(monkeypatch):
    app = load_app(monkeypatch)
    client = TestClient(app)

    response = client.get(
        "/api/settings",
        headers={"Authorization": "Bearer test-admin-token"},
    )

    assert response.status_code == 200
    assert "wifi_hotspot_password" not in response.json()


def test_cors_rejects_unknown_origins(monkeypatch):
    app = load_app(monkeypatch)
    client = TestClient(app)

    response = client.options(
        "/api/proxies",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert response.headers.get("access-control-allow-origin") is None


def test_proxy_config_rejects_invalid_port():
    from app.config import ProxyConfig

    with pytest.raises(ValueError):
        ProxyConfig(id="proxy_bad", host="127.0.0.1", port=70000)


def test_hotspot_config_requires_the_windows_ics_subnet():
    from app.config import AppConfig

    with pytest.raises(ValueError, match="192.168.137.0/24"):
        AppConfig(
            wifi_hotspot_enabled=True,
            lan_gateway_ip="192.168.10.1",
            dhcp_range_start="192.168.10.10",
            dhcp_range_end="192.168.10.250",
        )


def test_update_url_only_allows_c69_https_hosts():
    from app.update_manager import _validate_update_url

    assert _validate_update_url("https://cdn.c69.us/c69-router.zip")
    with pytest.raises(ValueError):
        _validate_update_url("http://cdn.c69.us/c69-router.zip")
    with pytest.raises(ValueError):
        _validate_update_url("https://evil.example/c69-router.zip")
