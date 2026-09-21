"""Local browser connection must not remove the supplier API's authentication boundary."""

import pytest
from fastapi.testclient import TestClient

from environment_harness import EnvironmentSession, EvidenceStore, Principal, local_viewer
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.local_viewer import LocalViewerLogin
from environment_harness.server import create_app

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def local_service(tmp_path):
    session = EnvironmentSession(EvidenceStore(tmp_path), SyntheticEnvironment())
    principal = Principal(tenant="local", subject="researcher", role="researcher")
    login = LocalViewerLogin(ORIGIN, session.store.issue(principal))
    app = create_app(session, local_login=login)
    return session, login, app


def test_one_time_connection_keeps_api_authenticated(local_service):
    session, login, app = local_service
    client = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))
    headers = {"Origin": ORIGIN, "X-Local-Login": login.ticket}
    assert client.get("/v1/environments").status_code == 401
    html = client.get("/").text
    assert login.ticket not in html and login.credential not in html
    response = client.post("/local/connect", headers=headers)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    credential = response.json()["token"]
    assert session.store.authenticate(credential).tenant == "local"
    assert (
        client.get("/v1/environments", headers={"Authorization": "Bearer " + credential}).status_code == 200
    )
    assert client.post("/local/connect", headers=headers).status_code == 403
    assert client.get("/v1/environments").status_code == 401


@pytest.mark.parametrize(
    "origin,base_url,peer,ticket",
    [
        ("https://attacker.invalid", ORIGIN, "127.0.0.1", "valid"),
        (None, ORIGIN, "127.0.0.1", "valid"),
        ("http://attacker.invalid", "http://attacker.invalid", "127.0.0.1", "valid"),
        (ORIGIN, ORIGIN, "203.0.113.1", "valid"),
        (ORIGIN, ORIGIN, "127.0.0.1", "wrong"),
    ],
)
def test_local_connection_rejects_other_origins_hosts_peers_and_tickets(
    local_service,
    origin,
    base_url,
    peer,
    ticket,
):
    _, login, app = local_service
    client = TestClient(app, base_url=base_url, client=(peer, 50000))
    headers = {"X-Local-Login": login.ticket if ticket == "valid" else ticket, "X-Forwarded-For": "127.0.0.1"}
    if origin is not None:
        headers["Origin"] = origin
    assert client.post("/local/connect", headers=headers).status_code == 403
    assert login.ticket


def test_expired_connection_link(local_service):
    _, login, app = local_service
    login.expires = 0
    client = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))
    response = client.post("/local/connect", headers={"Origin": ORIGIN, "X-Local-Login": login.ticket})
    assert response.status_code == 403


def test_supplier_service_has_no_local_connection_endpoint(local_service):
    session, login, _ = local_service
    client = TestClient(create_app(session), base_url=ORIGIN, client=("127.0.0.1", 50000))
    response = client.post("/local/connect", headers={"Origin": ORIGIN, "X-Local-Login": login.ticket})
    assert response.status_code == 404
    assert client.get("/v1/environments").status_code == 401


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

    login = local_viewer.LocalViewerLogin("http://localhost", "credential")
    server = type("Server", (), {"started": True, "should_exit": False})()
    monkeypatch.setattr(local_viewer, "open_browser", lambda _url: False)
    local_viewer.open_when_ready(server, login)
    assert "Could not open a browser" in capsys.readouterr().out

    server = type("Server", (), {"started": False, "should_exit": True})()
    local_viewer.open_when_ready(server, login)

    server = type("Server", (), {"started": True, "should_exit": False})()
    monkeypatch.setattr(local_viewer, "open_browser", lambda _url: (_ for _ in ()).throw(OSError()))
    local_viewer.open_when_ready(server, login)
    assert "Could not open a browser" in capsys.readouterr().out
