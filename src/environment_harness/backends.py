"""Optional isolated workers. Environment and scorer credentials never enter agents."""

import json
import re
import subprocess

from .errors import Conflict, Forbidden


def identity(value):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value):
        raise ValueError("invalid worker identity")
    return value


class DockerBackend:
    def __init__(self, image):
        if "@sha256:" not in image:
            raise ValueError("worker images must be pinned by digest")
        self.image = image

    def start(self, command, *, identity, limits):
        name = "environment-harness-" + globals()["identity"](identity)
        if limits.get("network", "none") != "none":
            raise Forbidden("networked agents require a separately configured scoped gateway")
        args = [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "environment-harness=true",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--pids-limit",
            str(limits.get("pids", 128)),
            "--memory",
            str(limits.get("memory", "512m")),
            "--cpus",
            str(limits.get("cpus", 1)),
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--workdir=/tmp",
            self.image,
            *command,
        ]
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise Conflict("Docker worker could not start")
        return result.stdout.strip()

    def _check(self, handle):
        if not re.fullmatch(r"[a-f0-9]{12,64}", handle):
            raise Forbidden("invalid worker handle")
        result = subprocess.run(
            ["docker", "inspect", handle], capture_output=True, text=True, timeout=15, check=True
        )
        info = json.loads(result.stdout)[0]
        if info["Config"].get("Labels", {}).get("environment-harness") != "true":
            raise Forbidden("worker is not owned by this backend")
        return info

    def status(self, handle):
        state = self._check(handle)["State"]
        return {"status": state["Status"], "exit_code": state["ExitCode"]}

    def stop(self, handle):
        self._check(handle)
        subprocess.run(["docker", "stop", "--time", "5", handle], capture_output=True, timeout=15, check=True)


class ModalBackend:
    def __init__(self, app, image, *, name_prefix="environment-harness"):
        self.app, self.image, self.name_prefix = app, image, name_prefix

    def start(self, command, *, identity, limits):
        import modal

        globals()["identity"](identity)
        sandbox = modal.Sandbox.create(
            *command,
            app=self.app,
            image=self.image,
            name=f"{self.name_prefix}-{identity}",
            timeout=int(limits.get("timeout", 300)),
            cpu=float(limits.get("cpus", 1)),
            memory=int(limits.get("memory_mb", 512)),
            block_network=True,
        )
        return sandbox.object_id

    def status(self, handle):
        import modal

        sandbox = modal.Sandbox.from_id(handle)
        code = sandbox.poll()
        return {"status": "running" if code is None else "exited", "exit_code": code}

    def stop(self, handle):
        import modal

        modal.Sandbox.from_id(handle).terminate()
