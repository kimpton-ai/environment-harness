"""Local browser connection must not remove the supplier API's authentication boundary."""

import pytest
from _credentials import bearer
from fastapi.testclient import TestClient

from environment_harness import EvidenceStore, local_viewer
from environment_harness.access import _AccessContext
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.local_viewer import LocalViewerAccess
from environment_harness.runtime import _SessionRuntime
from environment_harness.server import create_app

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def local_service(tmp_path):
    session = _SessionRuntime(EvidenceStore(tmp_path), SyntheticEnvironment())
    principal = _AccessContext(tenant="local", subject="researcher", policy="trusted-local")
    access = LocalViewerAccess(ORIGIN, bearer(session.store, principal))
    app = create_app(session, local_access=access)
    return session, access, app


def test_local_viewer_reconnects_while_api_stays_authenticated(local_service):
    session, access, app = local_service
    client = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))
    headers = {"Origin": ORIGIN}
    assert client.get("/v1/sessions").status_code == 401
    html = client.get("/").text
    assert access.credential not in html
    first = client.post("/local/connect", headers=headers)
    second = client.post("/local/connect", headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json() == second.json()
    credential = first.json()["token"]
    assert session.store.authenticate(credential).tenant == "local"
    assert client.get("/v1/sessions", headers={"Authorization": "Bearer " + credential}).status_code == 200
    assert client.get("/v1/sessions").status_code == 401
    assert client.get("/viewer/config").json() == {"authentication": "local"}


@pytest.mark.parametrize(
    "origin,base_url,peer",
    [
        ("https://attacker.invalid", ORIGIN, "127.0.0.1"),
        (None, ORIGIN, "127.0.0.1"),
        ("http://attacker.invalid", "http://attacker.invalid", "127.0.0.1"),
        (ORIGIN, ORIGIN, "203.0.113.1"),
    ],
)
def test_local_connection_rejects_other_origins_hosts_and_peers(
    local_service,
    origin,
    base_url,
    peer,
):
    _, _, app = local_service
    client = TestClient(app, base_url=base_url, client=(peer, 50000))
    headers = {"X-Forwarded-For": "127.0.0.1"}
    if origin is not None:
        headers["Origin"] = origin
    assert client.post("/local/connect", headers=headers).status_code == 403


def test_supplier_service_has_no_local_connection_endpoint(local_service):
    session, _, _ = local_service
    client = TestClient(create_app(session), base_url=ORIGIN, client=("127.0.0.1", 50000))
    response = client.post("/local/connect", headers={"Origin": ORIGIN})
    assert response.status_code == 404
    assert client.get("/v1/sessions").status_code == 401
    assert client.get("/viewer/config").json() == {"authentication": "credential"}


def test_browser_opening_uses_explicit_known_browsers(monkeypatch):
    attempts = []
    monkeypatch.setattr(local_viewer.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_viewer.subprocess,
        "run",
        lambda command, **_kwargs: attempts.append(command) or type("Result", (), {"returncode": 0})(),
    )
    assert local_viewer.open_browser("http://localhost")
    assert attempts == [["open", "-a", "Google Chrome", "http://localhost"]]

    monkeypatch.setattr(local_viewer.sys, "platform", "linux")
    calls = []

    class Browser:
        def __init__(self, name):
            self.name = name

        def open(self, url):
            calls.append((self.name, url))
            return self.name == "firefox"

    def browser(name):
        if name == "chrome":
            raise local_viewer.webbrowser.Error("not installed")
        return Browser(name)

    monkeypatch.setattr(local_viewer.webbrowser, "get", browser)
    assert local_viewer.open_browser("http://localhost")
    assert calls[-1][0] == "firefox"


def test_browser_opening_and_readiness_fail_safely(monkeypatch, capsys):
    monkeypatch.setattr(local_viewer.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_viewer.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
    )
    assert not local_viewer.open_browser("http://localhost")

    server = type("Server", (), {"started": True, "should_exit": False})()
    monkeypatch.setattr(local_viewer, "open_browser", lambda _url: False)
    local_viewer.open_when_ready(server, "http://localhost/home")
    assert capsys.readouterr().out == ("Could not open a browser. Open http://localhost/home manually.\n")

    server = type("Server", (), {"started": False, "should_exit": True})()
    local_viewer.open_when_ready(server, "http://localhost/home")

    server = type("Server", (), {"started": True, "should_exit": False})()
    monkeypatch.setattr(local_viewer, "open_browser", lambda _url: (_ for _ in ()).throw(OSError()))
    local_viewer.open_when_ready(server, "http://localhost/home")
    assert "Could not open a browser" in capsys.readouterr().out
