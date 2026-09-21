"""Exercise the EnvironmentHarness viewer through its public browser interface."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from environment_harness import EnvironmentHarness, EvidenceStore, Principal, Scenario
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.showcase import create_synthetic_showcase


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for(url: str, deadline: float) -> None:
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {url}")
        time.sleep(0.05)


def create_sessions(root: Path) -> None:
    store = EvidenceStore(root)
    who = Principal(tenant="local", subject="browser-test", role="researcher")
    participants = (
        "alice",
        "bob",
        "charlie",
        "dana",
        "erin",
        "frank",
        "grace",
        "heidi",
        "ivan",
        "judy",
        "mallory",
        "oscar",
    )
    for turns in (24, 18, 12):
        create_synthetic_showcase(store, who, turns=turns, participants=participants)
    EnvironmentHarness(
        store,
        environment_factory=SyntheticEnvironment,
        agent_factories={"agent": SyntheticAgent},
        max_concurrency=2,
    ).experiment(
        "Support response evaluation",
        [
            Scenario(id="easy-case", input={}, metadata={"name": "Routine request"}),
            Scenario(id="hard-case", input={}, metadata={"name": "Escalated request"}),
        ],
        trials=2,
        turns=2,
    ).run()


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
        raise SystemExit("a Chrome or Chromium executable is required for the browser UI check")

    with tempfile.TemporaryDirectory(prefix="environment-browser-ui-") as directory:
        root = Path(directory)
        store = root / "evidence"
        create_sessions(store)
        server_port = available_port()
        debug_port = available_port()
        origin = f"http://127.0.0.1:{server_port}"
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "environment_harness.cli",
                "--store",
                str(store),
                "serve",
                "--port",
                str(server_port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        browser = None
        try:
            wait_for(origin + "/health", time.monotonic() + 20)
            browser = subprocess.Popen(
                [
                    chrome,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--no-first-run",
                    "--no-proxy-server",
                    f"--remote-debugging-port={debug_port}",
                    f"--user-data-dir={root / 'profile'}",
                    "about:blank",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_for(f"http://127.0.0.1:{debug_port}/json/version", time.monotonic() + 10)
            subprocess.run(
                [
                    "node",
                    "scripts/browser_ui_test.mjs",
                    origin,
                    str(debug_port),
                ],
                check=True,
                env=os.environ.copy(),
            )
        finally:
            if browser is not None:
                browser.terminate()
                browser.wait(timeout=5)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
