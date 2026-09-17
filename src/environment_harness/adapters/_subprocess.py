"""Cross-platform subprocess group lifecycle helpers."""

import os
import signal
import subprocess
import time
from contextlib import suppress
from typing import Any


def popen_group(command, **kwargs: Any) -> subprocess.Popen[Any]:
    """Start a binary-mode subprocess in its own platform process group."""
    if os.name == "nt":
        return subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **kwargs)
    return subprocess.Popen(command, start_new_session=True, **kwargs)


def terminate_tree(process, *, grace=1):
    """Stop a subprocess and its descendants, then reap its leader."""
    if os.name == "nt":
        # Python cannot signal an arbitrary Windows process group. taskkill /T is
        # the platform facility that includes descendants created by the worker.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        with suppress(OSError):
            process.kill()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return

    # The group may outlive its leader. Always clean descendants, including after success.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except (PermissionError, ProcessLookupError):
            break
        time.sleep(0.02)
    with suppress(PermissionError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
