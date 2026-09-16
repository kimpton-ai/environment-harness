"""An offline synthetic agent: one JSON observation in, one JSON action out."""

import json
import sys

observation = json.load(sys.stdin)
# An external program receives only its delivered observation, not the environment store.
total = observation["payload"]["total"]
json.dump({"value": 1 if total < 2 else -1}, sys.stdout)
sys.stdout.write("\n")
