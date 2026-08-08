from __future__ import annotations

import os
import secrets
import time
from html import escape
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

@dataclass
class StoredSession:
    user: dict
    id_token: str | None
    expires_at: float

sessions: dict[str, StoredSession] = {}
MAX_SESSIONS = 10_000
oauth = OAuth()
oauth.register(
    name="tuurio",
    client_id=os.getenv("TUURIO_CLIENT_ID", ""),
    client_secret=os.getenv("TUURIO_CLIENT_SECRET") or None,
    server_metadata_url=f"{os.getenv('TUURIO_ISSUER', '').rstrip('/')}/.well-known/openid-configuration" if os.getenv("TUURIO_ISSUER") else None,
    client_kwargs={"scope": os.getenv("TUURIO_SCOPE", "openid profile email"), "code_challenge_method": "S256"},
)

app = FastAPI(title="Tuurio ID FastAPI starter")
COOKIE_SECURE = os.getenv("TUURIO_COOKIE_SECURE", "false").lower() == "true"
app.add_middleware(SessionMiddleware, secret_key=os.getenv("TUURIO_SESSION_SECRET", "development-only-change-before-deploy"), same_site="lax", https_only=COOKIE_SECURE)

def validate_config() -> None:
    issuer = os.getenv("TUURIO_ISSUER", "").strip()
    client_id = os.getenv("TUURIO_CLIENT_ID", "").strip()
    if not issuer or "YOUR_" in issuer or not client_id or client_id.startswith("YOUR_"):
        raise RuntimeError("TUURIO_ISSUER and TUURIO_CLIENT_ID must be configured.")
    parsed = urlparse(issuer)
    issuer_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and issuer_loopback):
        raise RuntimeError("TUURIO_ISSUER must use HTTPS outside an explicit loopback host.")
    for name, value in (("TUURIO_REDIRECT_URI", os.getenv("TUURIO_REDIRECT_URI", "http://localhost:8000/auth/callback")), ("TUURIO_POST_LOGOUT_REDIRECT_URI", os.getenv("TUURIO_POST_LOGOUT_REDIRECT_URI", "http://localhost:8000/logout/callback"))):
        parsed = urlparse(value)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise RuntimeError(f"{name} must use HTTPS outside an explicit loopback host.")

def current_session(request: Request) -> StoredSession | None:
    prune_expired_sessions()
    session_id = request.cookies.get("tuurio_session")
    stored = sessions.get(session_id or "")
    if stored and stored.expires_at > time.time():
        return stored
    if session_id:
        sessions.pop(session_id, None)
    return None

def prune_expired_sessions() -> None:
    now = time.time()
    for session_id, stored in list(sessions.items()):
        if stored.expires_at <= now:
            sessions.pop(session_id, None)

def page(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title><style>body{{font-family:system-ui;margin:4rem auto;max-width:42rem;padding:0 1rem;color:#0f172a}}a.button{{display:inline-block;padding:.7rem 1rem;background:#0d6efd;color:#fff;border-radius:.5rem;text-decoration:none}}a{{color:#0d6efd}}</style></head><body>{content}</body></html>')

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    stored = current_session(request)
    action = '<a class="button" href="/dashboard">Open dashboard</a>' if stored else '<a class="button" href="/auth/login">Sign in with Tuurio ID</a>'
    return page("Tuurio ID · FastAPI", f"<p>EU-hosted identity</p><h1>FastAPI + Tuurio ID</h1><p>Async OpenID Connect with PKCE and opaque sessions.</p>{action}")

@app.get("/auth/login")
async def login(request: Request):
    validate_config()
    redirect_uri = os.getenv("TUURIO_REDIRECT_URI", "http://localhost:8000/auth/callback")
    return await oauth.tuurio.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def callback(request: Request):
    token = await oauth.tuurio.authorize_access_token(request)
    claims = token.get("userinfo") or {}
    if not token.get("access_token") or not claims.get("sub"):
        return HTMLResponse("Validated identity is missing.", status_code=400)
    metadata = await oauth.tuurio.load_server_metadata()
    endpoint = metadata.get("userinfo_endpoint")
    if not endpoint:
        return HTMLResponse("UserInfo endpoint is missing.", status_code=400)
    response = await oauth.tuurio.get(endpoint, token=token)
    response.raise_for_status()
    profile = response.json()
    if profile.get("sub") != claims["sub"]:
        return HTMLResponse("UserInfo subject mismatch.", status_code=400)
    prune_expired_sessions()
    if len(sessions) >= MAX_SESSIONS:
        return HTMLResponse("Too many active sessions; try again later.", status_code=503)
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = StoredSession(
        user={key: profile.get(key) for key in ("sub", "name", "email")},
        id_token=token.get("id_token"),
        expires_at=time.time() + min(int(token.get("expires_in", 3600)), 3600),
    )
    request.session.clear()
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("tuurio_session", session_id, httponly=True, samesite="lax", secure=COOKIE_SECURE or request.url.scheme == "https", max_age=3600, path="/")
    return response

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    stored = current_session(request)
    if not stored:
        return RedirectResponse("/", status_code=303)
    user = stored.user
    label = escape(str(user.get("name") or user.get("email") or user["sub"]))
    return page("Protected dashboard", f'<p>Protected area</p><h1>Welcome {label}</h1><p>The browser stores only an opaque session identifier.</p><a href="/auth/logout">Sign out</a>')

@app.get("/auth/logout")
async def logout(request: Request):
    validate_config()
    session_id = request.cookies.get("tuurio_session")
    stored = sessions.pop(session_id or "", None)
    metadata = await oauth.tuurio.load_server_metadata()
    endpoint = metadata.get("end_session_endpoint")
    target = "/" if not endpoint else f"{endpoint}?{urlencode({'post_logout_redirect_uri': os.getenv('TUURIO_POST_LOGOUT_REDIRECT_URI', 'http://localhost:8000/logout/callback'), **({'id_token_hint': stored.id_token} if stored and stored.id_token else {})})}"
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie("tuurio_session", path="/")
    return response

@app.get("/logout/callback")
async def logout_callback(request: Request):
    request.session.clear()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("tuurio_session", path="/")
    return response
