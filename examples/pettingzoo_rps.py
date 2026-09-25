"""Run the independently maintained public RPS environment with synthetic agents."""

from tempfile import TemporaryDirectory

from pettingzoo.classic import rps_v2

from environment_harness import EnvironmentHarness, EvidenceStore, Scenario
from environment_harness.adapters.environments import PettingZooParallel


class Rock:
    implementation = "synthetic-rock@1"

    def act(self, observation):
        return {"action": 0}


with TemporaryDirectory(prefix="environment-harness-public-") as root:
    native = rps_v2.parallel_env(max_cycles=3)
    try:
        harness = EnvironmentHarness(
            EvidenceStore(root),
            environment=lambda: PettingZooParallel(native, name="public-rps", version="pettingzoo-1.25.0"),
            agents={player: Rock for player in native.possible_agents},
        )
        session = harness.run(Scenario(id="public-rps", input={}), turns=3)
        record = session.record()
        assert record["revision"] == 3 and record["status"] == "completed"
        print(
            {"public_adapter": "PettingZoo RPS", "revision": record["revision"], "status": record["status"]}
        )
    finally:
        native.close()
