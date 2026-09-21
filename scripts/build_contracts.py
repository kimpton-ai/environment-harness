import json
import sys
from pathlib import Path

from environment_harness import contracts, motor_contracts

root = Path(__file__).resolve().parents[1]
models = (
    "Capabilities",
    "EnvironmentSpec",
    "AgentSpec",
    "RunPolicy",
    "ExperimentSpec",
    "Principal",
    "Action",
    "Transition",
    "Finding",
    "MetricDefinition",
    "ScoreReport",
)
for name in models:
    schema = getattr(contracts, name).model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:environment-harness:v1:" + name
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    path = root / "contracts" / (name + ".schema.json")
    if "--check" in sys.argv:
        if not path.exists() or path.read_text() != text:
            raise SystemExit("contract drift: " + name)
    else:
        path.write_text(text)

for name in (
    "MotorProfile",
    "MotorRequest",
    "MotorStep",
    "MotorCandidate",
    "MotorSelection",
    "MotorReceipt",
):
    schema = getattr(motor_contracts, name).model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:environment-harness:v1:" + name
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    path = root / "contracts" / (name + ".schema.json")
    if "--check" in sys.argv:
        if not path.exists() or path.read_text() != text:
            raise SystemExit("contract drift: " + name)
    else:
        path.write_text(text)
