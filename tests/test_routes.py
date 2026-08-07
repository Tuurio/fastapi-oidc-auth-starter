from fastapi.testclient import TestClient
import pytest

from app.main import app, validate_config

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
