"""Run a custom local JSON program through the command-agent boundary."""

import argparse
import json
import sys
from pathlib import Path

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.adapters.programs import CommandAgent
from environment_harness.contracts import RunPolicy
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runner import run


def main(directory):
    program = Path(__file__).with_name("command_agent.py").resolve()
    environment = SyntheticEnvironment()
    session = EnvironmentSession(EvidenceStore(directory), environment)
    who = Principal(tenant="local", subject="example", role="researcher")
    spec = ExperimentSpec(
        environment=environment.spec,
        participants=(AgentSpec(id="custom", implementation="threshold-command@1", policy_version="1"),),
        policy=RunPolicy(max_turns=4),
    )
    environment = session.create(spec, who)["id"]
    agent = CommandAgent([sys.executable, str(program)], "threshold-command@1")
    result = run(session, environment, who, {"custom": agent}, turns=4)
    print(
        json.dumps(
            {
                "environment": environment,
                "revision": result["revision"],
                "status": result["status"],
                "synthetic": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".local/custom-agent")
    main(parser.parse_args().store)
