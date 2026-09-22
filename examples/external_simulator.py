"""Tiny JSON-lines simulator used by the external environment example.

This process deliberately has no EnvironmentHarness dependency. It represents a
game server, simulator, engine bridge, or device service owned by another package.
"""

from __future__ import annotations

import json
import os
import sys


def respond(result):
    print(json.dumps({"result": result}, separators=(",", ":")), flush=True)


def main():
    positions: dict[str, list[int]] = {}
    receipts: dict[str, dict] = {}
    for line in sys.stdin:
        request = json.loads(line)
        method = request["method"]
        arguments = request.get("arguments", {})
        if method == "hello":
            respond({"protocol": "example-simulator.v1", "worker_pid": os.getpid()})
        elif method == "move":
            operation_id = arguments["operation_id"]
            if operation_id not in receipts:
                entity = arguments["entity"]
                delta = arguments["delta"]
                current = positions.get(entity, [0, 0, 0])
                position = [current[index] + delta[index] for index in range(3)]
                positions[entity] = position
                receipts[operation_id] = {
                    "operation_id": operation_id,
                    "cost_micros": 0,
                    "status": "moved",
                    "entity": entity,
                    "delta": delta,
                    "position": position,
                    "worker_pid": os.getpid(),
                }
            respond(receipts[operation_id])
        elif method == "lookup":
            respond(receipts.get(arguments["operation_id"]))
        elif method == "shutdown":
            respond({"status": "stopped"})
            return
        else:
            raise ValueError(f"unknown simulator method: {method}")


if __name__ == "__main__":
    main()
