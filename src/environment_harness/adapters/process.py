"""Persistent JSON worker protocol for trusted supplier code in another process.

Use an OS container boundary for untrusted programs. No pickle or arbitrary memory
snapshot is accepted. The environment still serializes its own durable state.
"""

import json
import os
import selectors
import signal
import subprocess
import threading
import time

from ..contracts import EnvironmentSpec, Transition
from ..errors import Unavailable
from ..store import encode


class ProcessEnvironment:
    def __init__(self, command, *, timeout=30, max_bytes=16777216):
        self.timeout, self.max_bytes = timeout, max_bytes
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "PYTHON_DOTENV_DISABLED": "1"},
            start_new_session=True,
        )
        self.spec = EnvironmentSpec.model_validate(self._call("spec", {}))

    def _call(self, method, arguments):
        with self.lock:
            payload = (encode({"method": method, "arguments": arguments}) + "\n").encode()
            if len(payload) > self.max_bytes:
                raise Unavailable("environment request exceeds limit")
            selector = selectors.DefaultSelector()
            try:
                stdin, stdout = self.process.stdin, self.process.stdout
                if stdin is None or stdout is None:
                    raise Unavailable("environment worker pipes are unavailable")
                os.set_blocking(stdin.fileno(), False)
                selector.register(stdin, selectors.EVENT_WRITE)
                sent = 0
                result = bytearray()
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                        if key.fileobj == stdin:
                            sent += os.write(stdin.fileno(), payload[sent : sent + 65536])
                            if sent == len(payload):
                                selector.unregister(stdin)
                                selector.register(stdout, selectors.EVENT_READ)
                        else:
                            chunk = os.read(stdout.fileno(), 65536)
                            if not chunk:
                                raise Unavailable("environment worker exited")
                            result.extend(chunk)
                            if len(result) > self.max_bytes:
                                raise Unavailable("environment response exceeds limit")
                            if b"\n" in result:
                                response = json.loads(result)
                                if "error" in response:
                                    raise Unavailable("environment worker rejected request")
                                return response["result"]
                raise Unavailable("environment worker deadline exceeded")
            except BaseException:
                self.close()
                raise
            finally:
                selector.close()

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
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
