import json
import sys
from pathlib import Path

from environment_harness import contracts, resources, training, trajectories

root = Path(__file__).resolve().parents[1]
models = (
    (contracts, "Capabilities"),
    (contracts, "Scenario"),
    (contracts, "OperationSpec"),
    (contracts, "EnvironmentSpec"),
    (contracts, "AgentSpec"),
    (contracts, "RunPolicy"),
    (contracts, "ExperimentSpec"),
    (contracts, "ActivityEvent"),
    (contracts, "ActivityPage"),
    (contracts, "ActivitySession"),
    (contracts, "ActivityScenario"),
    (contracts, "ActivityExperiment"),
    (contracts, "ActivityHierarchy"),
    (contracts, "BranchRequest"),
    (contracts, "Action"),
    (contracts, "Transition"),
    (contracts, "Finding"),
    (contracts, "MetricDefinition"),
    (contracts, "ScoreReport"),
    (trajectories, "SourceRegistration"),
    (trajectories, "SourceRecord"),
    (trajectories, "SourceAcknowledgement"),
    (trajectories, "SourceIngestionBatch"),
    (trajectories, "SourceStatus"),
    (trajectories, "SourceStatusUpdate"),
    (resources, "ScenarioSet"),
    (resources, "Experiment"),
    (resources, "Session"),
    (resources, "Checkpoint"),
    (trajectories, "Policy"),
    (trajectories, "Trajectory"),
    (trajectories, "TrajectorySnapshot"),
    (training, "TrajectoryDataset"),
    (training, "TrainingRun"),
)
for module, name in models:
    schema = getattr(module, name).model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:environment-harness:v1:" + name
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    path = root / "contracts" / (name + ".schema.json")
    if "--check" in sys.argv:
        if not path.exists() or path.read_text() != text:
            raise SystemExit("contract drift: " + name)
    else:
        path.write_text(text)
