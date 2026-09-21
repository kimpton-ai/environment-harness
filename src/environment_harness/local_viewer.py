"""Single-use browser connection for the loopback CLI service."""

import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field

from .errors import Forbidden


@dataclass
class LocalViewerLogin:
    origin: str
    credential: str = field(repr=False)
    ticket: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    expires: float = field(default_factory=lambda: time.monotonic() + 300)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def redeem(self, ticket: str) -> str:
        with self._lock:
            if (
                not self.ticket
                or time.monotonic() >= self.expires
                or not secrets.compare_digest(ticket, self.ticket)
            ):
                raise Forbidden("Local connection link expired or already used. Restart with serve --open.")
            self.ticket = ""
            return self.credential


def open_browser(url: str) -> bool:
    # Select known browsers explicitly rather than the system's default URL handler.
    if sys.platform == "darwin":
        for browser in ("Google Chrome", "Safari"):
            result = subprocess.run(
                ["open", "-a", browser, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return True
    else:
        for browser in ("chrome", "chromium", "firefox", "msedge"):
            try:
                if webbrowser.get(browser).open(url):
                    return True
            except webbrowser.Error:
                continue
    return False


def open_when_ready(server, login: LocalViewerLogin):
    deadline = time.monotonic() + 30
    while not server.started:
        if server.should_exit or time.monotonic() >= deadline:
            return
        time.sleep(0.05)
    try:
        opened = open_browser(f"{login.origin}/#local-login={login.ticket}")
    except OSError:
        opened = False
    if not opened:
        print("Could not open a browser. Open the viewer and use the printed credential file.", flush=True)
