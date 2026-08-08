from fastapi.testclient import TestClient
import pytest
from unittest.mock import AsyncMock, Mock

from app.main import StoredSession, app, oauth, sessions, validate_config

client = TestClient(app)

def test_home_is_available_without_configuration():
    response = client.get("/")
    assert response.status_code == 200
    assert "FastAPI + Tuurio ID" in response.text

def test_dashboard_requires_opaque_session():
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

def test_rejects_cleartext_non_loopback_redirect(monkeypatch):
    monkeypatch.setenv("TUURIO_ISSUER", "https://test.id.tuurio.com")
    monkeypatch.setenv("TUURIO_CLIENT_ID", "test-client")
    monkeypatch.setenv("TUURIO_REDIRECT_URI", "http://example.com/auth/callback")
    with pytest.raises(RuntimeError, match="must use HTTPS"):
        validate_config()

def test_callback_rejects_userinfo_subject_mismatch(monkeypatch):
    monkeypatch.setattr(oauth.tuurio, "authorize_access_token", AsyncMock(return_value={
        "access_token": "server-side-token",
        "userinfo": {"sub": "validated-subject"},
    }))
    monkeypatch.setattr(oauth.tuurio, "load_server_metadata", AsyncMock(return_value={
        "userinfo_endpoint": "https://issuer.example/userinfo",
    }))
    userinfo_response = Mock()
    userinfo_response.json.return_value = {"sub": "different-subject"}
    monkeypatch.setattr(oauth.tuurio, "get", AsyncMock(return_value=userinfo_response))

    response = client.get("/auth/callback")

    assert response.status_code == 400
    assert not sessions

def test_logout_removes_server_session_before_redirect(monkeypatch):
    monkeypatch.setenv("TUURIO_ISSUER", "https://issuer.example")
    monkeypatch.setenv("TUURIO_CLIENT_ID", "client-id")
    monkeypatch.setattr(oauth.tuurio, "load_server_metadata", AsyncMock(return_value={
        "end_session_endpoint": "https://issuer.example/logout",
    }))
    sessions["opaque-session"] = StoredSession(
        user={"sub": "subject"},
        id_token="server-side-id-token",
        expires_at=9_999_999_999,
    )
    client.cookies.set("tuurio_session", "opaque-session")

    response = client.get("/auth/logout", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://issuer.example/logout?")
    assert "opaque-session" not in sessions
    client.cookies.clear()
