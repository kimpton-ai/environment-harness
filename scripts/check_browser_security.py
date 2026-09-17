"""Exercise the local viewer in a real headless browser on Linux CI."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


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
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "environment_harness.cli",
                "--store",
                str(root / "evidence"),
                "serve",
                "--port",
                str(port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHON_DOTENV_DISABLED": "1"},
        )
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
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("viewer server did not become ready") from None
                    time.sleep(0.05)

            browser = subprocess.Popen(
                [
                    chrome,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--no-first-run",
                    "--virtual-time-budget=5000",
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
                raise RuntimeError("headless browser did not render the credential-gated viewer")
            if "Bearer " in browser_stdout or "local-login=" in browser_stdout:
                raise RuntimeError("viewer HTML exposed a credential")

            try:
                request(origin + "/health", origin="https://attacker.invalid")
            except urllib.error.HTTPError as error:
                if error.code != 403 or error.read() != b'{"error":"cross_origin_denied"}':
                    raise RuntimeError("cross-origin browser request did not fail closed") from error
            else:
                raise RuntimeError("cross-origin browser request was accepted")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    print("headless browser security check passed")


if __name__ == "__main__":
    main()
