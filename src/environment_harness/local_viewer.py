"""Automatic browser access for the loopback CLI service."""

import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LocalViewerAccess:
    origin: str
    credential: str = field(repr=False)


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


def open_when_ready(server, url: str):
    deadline = time.monotonic() + 30
    while not server.started:
        if server.should_exit or time.monotonic() >= deadline:
            return
        time.sleep(0.05)
    try:
        opened = open_browser(url)
    except OSError:
        opened = False
    if not opened:
        print(f"Could not open a browser. Open {url} manually.", flush=True)
