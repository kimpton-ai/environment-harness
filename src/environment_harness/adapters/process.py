"""Persistent JSON worker protocol for trusted supplier code in another process.

Use an OS container boundary for untrusted programs. No pickle or arbitrary memory
snapshot is accepted. The environment still serializes its own durable state.
"""

import json
import os
import queue
import subprocess
import threading
import time

from ..contracts import EnvironmentSpec, Transition
from ..errors import Unavailable
from ..store import encode
from ._subprocess import popen_group, terminate_tree


class ProcessEnvironment:
    def __init__(self, command, *, timeout=30, max_bytes=16777216):
        self.timeout, self.max_bytes = timeout, max_bytes
        self.lock = threading.Lock()
        self.process = popen_group(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "PYTHON_DOTENV_DISABLED": "1"},
        )
        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self._read_responses, daemon=True)
        self.reader.start()
        self.spec = EnvironmentSpec.model_validate(self._call("spec", {}))

    def _read_responses(self):
        stdout = self.process.stdout
        if stdout is None:
            self.responses.put(Unavailable("environment worker output pipe is unavailable"))
            return
        try:
            while line := stdout.readline(self.max_bytes + 2):
                if len(line) > self.max_bytes or not line.endswith(b"\n"):
                    self.responses.put(Unavailable("environment response exceeds limit"))
                    return
                self.responses.put(line)
        except (OSError, ValueError):
            self.responses.put(Unavailable("environment worker output pipe failed"))
        finally:
            self.responses.put(None)

    def _call(self, method, arguments):
        with self.lock:
            payload = (encode({"method": method, "arguments": arguments}) + "\n").encode()
            if len(payload) > self.max_bytes:
                raise Unavailable("environment request exceeds limit")
            try:
                stdin, stdout = self.process.stdin, self.process.stdout
                if stdin is None or stdout is None:
                    raise Unavailable("environment worker pipes are unavailable")
                written = queue.Queue()

                def write_request():
                    try:
                        stdin.write(payload)
                        stdin.flush()
                        written.put(None)
                    except (OSError, ValueError) as exc:
                        written.put(exc)

                threading.Thread(target=write_request, daemon=True).start()
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    try:
                        write_result = written.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        if isinstance(write_result, Exception):
                            raise Unavailable("environment worker input pipe failed") from write_result
                    try:
                        line = self.responses.get(timeout=min(0.1, max(0, deadline - time.monotonic())))
                    except queue.Empty:
                        if self.process.poll() is not None:
                            raise Unavailable("environment worker exited")
                        continue
                    if line is None:
                        raise Unavailable("environment worker exited")
                    if isinstance(line, Exception):
                        raise line
                    response = json.loads(line)
                    if "error" in response:
                        raise Unavailable("environment worker rejected request")
                    return response["result"]
                raise Unavailable("environment worker deadline exceeded")
            except BaseException:
                self.close()
                raise

    def initialize(self, experiment):
        return self._call("initialize", {"experiment": experiment.model_dump(mode="json")})

    def observe(self, state, participant):
        return self._call("observe", {"state": state, "participant": participant})

    def resolve(self, state, actions, random, events):
        result = self._call(
            "resolve", {"state": state, "actions": actions, "rng": random.getstate(), "events": events}
        )
        from ..runtime import tuples

        random.setstate(tuples(result["rng"]))
        return Transition.model_validate(result["transition"])

    def intervene(self, state, changes):
        return self._call("intervene", {"state": state, "changes": changes})

    def close(self):
        if self.process.poll() is None:
            terminate_tree(self.process, grace=1)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self.reader.join(timeout=1)
