"""Supplier environment process entrypoint. stdout is exclusively JSON protocol."""

import json
import random
import sys

from .contracts import ExperimentSpec
from .plugins import environment
from .runtime import tuples
from .store import encode


def dispatch(env, method, args):
    if method == "spec":
        return env.spec.model_dump(mode="json")
    if method == "initialize":
        return env.initialize(ExperimentSpec.model_validate(args["experiment"]))
    if method == "observe":
        return env.observe(args["state"], args["participant"])
    if method == "intervene":
        return env.intervene(args["state"], args["changes"])
    if method == "resolve":
        rng = random.Random()
        rng.setstate(tuples(args["rng"]))
        transition = env.resolve(args["state"], args["actions"], rng, args["events"])
        return {"transition": transition.model_dump(mode="json"), "rng": rng.getstate()}
    raise ValueError("unknown worker method")


def main():
    env = environment(sys.argv[1] if len(sys.argv) > 1 else "synthetic-protocol")
    while raw := sys.stdin.buffer.readline(16777217):
        try:
            if len(raw) > 16777216 or not raw.endswith(b"\n"):
                raise ValueError("request framing")
            request = json.loads(raw)
            result = dispatch(env, request["method"], request["arguments"])
            print(encode({"result": result}), flush=True)
        except Exception:
            print(encode({"error": "worker_failure"}), flush=True)


if __name__ == "__main__":
    main()
