"""Deploy job viewer auth, mirroring the command viewer.

The browser opens /jobs/{id}/view (unauthed HTML shell), whose JS polls
/jobs/{id}/trace/ui. A browser cannot attach a Bearer header, so /trace/ui
must also accept the JWT from the HttpOnly cookie that /token sets on login.

Requirement: log in once (via Swagger's /token), then the deploy viewer works
in the browser; without logging in, /trace/ui stays 401.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def trace_client():
    app = create_app()
    with TestClient(app) as c:
        yield c


def _login(client):
    r = client.post("/token", data={"username": "test_admin", "password": "secret"})
    assert r.status_code == 200, r.text
    return r


def test_trace_ui_requires_auth(trace_client):
    # No cookie, no header -> rejected before any GitLab call.
    r = trace_client.get("/api/v1/deploy/jobs/123/trace/ui")
    assert r.status_code in (401, 403), r.text


def test_trace_ui_accepts_cookie_token(trace_client):
    _login(trace_client)  # cookie now in the client's jar
    # No Authorization header; the browser only has the cookie.
    r = trace_client.get("/api/v1/deploy/jobs/123/trace/ui")
    # Cookie satisfies auth+scope, so we get PAST auth. The downstream GitLab
    # call may fail (no live GitLab in tests), but it must not be a 401/403.
    assert r.status_code not in (401, 403), r.text


def test_trace_ui_accepts_bearer_header(trace_client):
    tok = _login(trace_client).json()["access_token"]
    trace_client.cookies.clear()  # exercise the header path only
    r = trace_client.get(
        "/api/v1/deploy/jobs/123/trace/ui",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code not in (401, 403), r.text


def test_view_shell_is_unauthed_html(trace_client):
    # The HTML shell itself stays open (same posture as the command viewer);
    # auth is enforced on the /trace/ui endpoint the page polls.
    r = trace_client.get("/api/v1/deploy/jobs/123/view")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
