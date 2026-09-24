import json
import sys
from pathlib import Path

from environment_harness import contracts

root = Path(__file__).resolve().parents[1]
models = (
    "Capabilities",
    "Scenario",
    "OperationSpec",
    "OperationSpecV2",
    "EnvironmentSpec",
    "EnvironmentSpecV2",
    "OperationRequest",
    "OperationPlan",
    "OperationReceipt",
    "AgentSpec",
    "RunPolicy",
    "ExperimentSpec",
    "ActivityEvent",
    "ActivityPage",
    "ActivitySession",
    "ActivityScenario",
    "ActivityExperiment",
    "ActivitySnapshot",
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
    version = (
        "v2"
        if name
        in {
            "OperationSpecV2",
            "EnvironmentSpecV2",
            "OperationRequest",
            "OperationPlan",
            "OperationReceipt",
            "ExperimentSpec",
        }
        else "v1"
    )
    schema["$id"] = f"urn:environment-harness:{version}:" + name
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    path = root / "contracts" / (name + ".schema.json")
    if "--check" in sys.argv:
        if not path.exists() or path.read_text() != text:
            raise SystemExit("contract drift: " + name)
    else:
        path.write_text(text)
