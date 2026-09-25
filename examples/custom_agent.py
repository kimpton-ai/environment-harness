"""Run a custom local JSON program through the command-agent boundary."""

import argparse
import json
import sys
from pathlib import Path

from environment_harness import EnvironmentHarness, EvidenceStore, Scenario
from environment_harness.adapters.programs import CommandAgent
from environment_harness.contracts import RunPolicy
from environment_harness.fixtures import SyntheticEnvironment


def main(directory):
    program = Path(__file__).with_name("command_agent.py").resolve()
    harness = EnvironmentHarness(
        EvidenceStore(directory),
        environment_factory=SyntheticEnvironment,
        agent_factories={
            "custom": lambda: CommandAgent([sys.executable, str(program)], "threshold-command@1")
        },
        policy=RunPolicy(max_turns=4),
    )
    session = harness.run(Scenario(id="command-agent", input={}), turns=4)
    record = session.record()
    print(
        json.dumps(
            {
                "environment": session.id,
                "revision": record["revision"],
                "status": record["status"],
                "synthetic": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".local/custom-agent")
    main(parser.parse_args().store)
