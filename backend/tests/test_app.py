from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.main import app

client = TestClient(app, raise_server_exceptions=False)


@app.get("/_test/boom", include_in_schema=False)
def boom():
    raise RuntimeError("secret detail")


def test_health_ok(monkeypatch):
    monkeypatch.setattr(db, "check_db", lambda: True)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["db"] == "ok" and body["timestamp"]


def test_health_db_down_returns_503(monkeypatch):
    monkeypatch.setattr(db, "check_db", lambda: False)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["db"] == "error"


def test_unknown_route_is_json_404():
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_500_shows_details_only_outside_production(monkeypatch):
    response = client.get("/_test/boom")
    assert response.status_code == 500
    assert "secret detail" in response.json()["error"]

    monkeypatch.setattr(get_settings(), "environment", "production")
    response = client.get("/_test/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}


def test_database_url_gets_psycopg_driver():
    settings = get_settings().model_copy(update={"database_url": "postgres://u:p@h:5432/d"})
    assert settings.sqlalchemy_database_url == "postgresql+psycopg://u:p@h:5432/d"
