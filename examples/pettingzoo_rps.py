"""Run the independently maintained public RPS environment with synthetic agents."""

from tempfile import TemporaryDirectory

from pettingzoo.classic import rps_v2

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.adapters.environments import PettingZooParallel
from environment_harness.runner import run


class Rock:
    def act(self, observation):
        return {"action": 0}


with TemporaryDirectory(prefix="environment-harness-public-") as root:
    native = rps_v2.parallel_env(max_cycles=3)
    try:
        environment = PettingZooParallel(native, name="public-rps", version="pettingzoo-1.25.0")
        spec = ExperimentSpec(
            environment=environment.spec,
            participants=tuple(
                AgentSpec(id=p, implementation="synthetic-rock@1", policy_version="1")
                for p in native.possible_agents
            ),
        )
        researcher = Principal(tenant="public-example", subject="researcher", role="researcher")
        session = EnvironmentSession(EvidenceStore(root), environment)
        environment = session.create(spec, researcher)["id"]
        result = run(session, environment, researcher, {p.id: Rock() for p in spec.participants}, turns=3)
        assert result["revision"] == 3 and result["status"] == "completed"
        print(
            {"public_adapter": "PettingZoo RPS", "revision": result["revision"], "status": result["status"]}
        )
    finally:
        native.close()
