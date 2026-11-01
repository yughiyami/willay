"""The web console may run on another local port: extra origins come from WILLAY_WEB_ORIGINS (names only, no wildcard)."""

from fastapi.testclient import TestClient

from willay.api.app import create_app, web_origins
from willay.api.dependencies import synthetic_dependencies


def test_default_origins_are_the_local_vite_ports():
    assert "http://localhost:5173" in web_origins({})


def test_extra_origins_are_added_from_the_environment():
    origins = web_origins({"WILLAY_WEB_ORIGINS": "http://localhost:4180, http://127.0.0.1:4180"})
    assert "http://localhost:4180" in origins and "http://127.0.0.1:4180" in origins


def test_wildcards_and_non_http_values_are_ignored():
    origins = web_origins({"WILLAY_WEB_ORIGINS": "*, javascript:alert(1), https://console.example.test"})
    assert "*" not in origins and "javascript:alert(1)" not in origins and "https://console.example.test" in origins


def test_a_preflight_from_an_extra_origin_is_allowed(monkeypatch):
    monkeypatch.setenv("WILLAY_WEB_ORIGINS", "http://localhost:4180")
    client = TestClient(create_app(synthetic_dependencies()))
    response = client.options("/health", headers={"Origin": "http://localhost:4180", "Access-Control-Request-Method": "GET"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:4180"
