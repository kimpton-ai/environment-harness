"""Supplier environment process entrypoint. stdout is exclusively JSON protocol."""

import json
import random
import sys

from .contracts import ExperimentSpec
from .plugins import environment
from .runtime import tuples
from .store import encode


def main():
    env = environment(sys.argv[1] if len(sys.argv) > 1 else "synthetic-protocol")
    while raw := sys.stdin.buffer.readline(16777217):
        try:
            if len(raw) > 16777216 or not raw.endswith(b"\n"):
                raise ValueError("request framing")
            request = json.loads(raw)
            method, args = request["method"], request["arguments"]
            if method == "spec":
                result = env.spec.model_dump(mode="json")
            elif method == "initialize":
                result = env.initialize(ExperimentSpec.model_validate(args["experiment"]))
            elif method == "observe":
                result = env.observe(args["state"], args["participant"])
            elif method == "intervene":
                result = env.intervene(args["state"], args["changes"])
            elif method == "resolve":
                rng = random.Random()
                rng.setstate(tuples(args["rng"]))
                transition = env.resolve(args["state"], args["actions"], rng, args["events"])
                result = {"transition": transition.model_dump(mode="json"), "rng": rng.getstate()}
            else:
                raise ValueError("unknown worker method")
            print(encode({"result": result}), flush=True)
        except Exception:
            print(encode({"error": "worker_failure"}), flush=True)


if __name__ == "__main__":
    main()
