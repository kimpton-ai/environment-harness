"""Exercise the local viewer in a real headless browser on Linux CI."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn

from environment_harness import EnvironmentHarness, EvidenceStore
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.server import create_app


def request(url: str, *, origin: str | None = None):
    headers = {"Origin": origin} if origin else {}
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=2)


def main() -> None:
    chrome = next(
        (
            candidate
            for candidate in (
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                shutil.which("chromium"),
                shutil.which("chromium-browser"),
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            )
            if candidate and Path(candidate).is_file()
        ),
        None,
    )
    if chrome is None:
        raise SystemExit("a Chrome or Chromium executable is required for the browser security check")

    with tempfile.TemporaryDirectory(prefix="environment-browser-") as directory:
        root = Path(directory)
        port = 18765
        origin = f"http://127.0.0.1:{port}"
        # A supplier deployment: no local viewer access and no ingestion capability.
        harness = EnvironmentHarness(
            EvidenceStore(root / "evidence"),
            environment=SyntheticEnvironment,
            agents={"alice": SyntheticAgent},
            reconcile=False,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(harness),
                host="127.0.0.1",
                port=port,
                access_log=False,
                proxy_headers=False,
                log_level="error",
            )
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with request(origin + "/health") as response:
                        assert response.status == 200
                        assert response.headers["Cache-Control"] == "no-store"
                        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
                    break
                except OSError:
                    if not server_thread.is_alive() or time.monotonic() >= deadline:
                        raise RuntimeError("viewer server did not become ready") from None
                    time.sleep(0.05)

            with request(origin + "/viewer/config") as response:
                assert json.load(response) == {"authentication": "credential"}
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        origin + "/local/connect",
                        headers={"Origin": origin},
                        method="POST",
                    ),
                    timeout=2,
                )
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise RuntimeError("supplier service exposed local viewer access") from error
            else:
                raise RuntimeError("supplier service exposed local viewer access")

            browser = subprocess.Popen(
                [
                    chrome,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--no-first-run",
                    "--no-proxy-server",
                    f"--user-data-dir={root / 'profile'}",
                    "--dump-dom",
                    origin + "/",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                start_new_session=True,
            )
            try:
                browser_stdout, browser_stderr = browser.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(browser.pid, signal.SIGKILL)
                browser_stdout, browser_stderr = browser.communicate()
            if browser.returncode not in (0, -signal.SIGKILL):
                raise RuntimeError(f"headless browser failed: {browser_stderr[-1000:]}")
            if "EnvironmentHarness" not in browser_stdout or 'type="password"' not in browser_stdout:
                raise RuntimeError("headless browser did not render the viewer")
            if re.search(r'<section id="access"[^>]*\shidden', browser_stdout):
                raise RuntimeError("supplier viewer hid its credential gate")
            if not re.search(r'<div id="workspace"[^>]*\shidden', browser_stdout):
                raise RuntimeError("supplier viewer exposed its workspace before authentication")
            if "Bearer " in browser_stdout or "local-login=" in browser_stdout:
                raise RuntimeError("viewer HTML exposed a credential")

            try:
                request(origin + "/health", origin="https://attacker.invalid")
            except urllib.error.HTTPError as error:
                payload = json.load(error)
                if error.code != 403 or payload.get("error", {}).get("code") != "cross_origin_denied":
                    raise RuntimeError("cross-origin browser request did not fail closed") from error
            else:
                raise RuntimeError("cross-origin browser request was accepted")
        finally:
            server.should_exit = True
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                raise RuntimeError("supplier viewer server did not stop")
    print("headless browser security check passed")


if __name__ == "__main__":
    main()
