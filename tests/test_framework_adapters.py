import pytest

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.adapters import frameworks
from environment_harness.contracts import RunPolicy
from environment_harness.errors import Unsupported
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.runner import run


def test_verifiers_031_bridge_projects_the_canonical_trajectory(monkeypatch, tmp_path):
    monkeypatch.setattr(frameworks, "version", lambda _package: "0.3.1")
    store = EvidenceStore(tmp_path)
    who = Principal(tenant="tenant", subject="researcher", role="researcher")
    environment = SyntheticEnvironment()
    session = EnvironmentSession(store, environment)
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(
                AgentSpec(id="alice", implementation=SyntheticAgent.implementation, policy_version="1"),
            ),
            purpose="training",
            split="training",
            policy=RunPolicy(max_turns=1),
        ),
        who,
    )["id"]
    run(session, environment_id, who, {"alice": SyntheticAgent()}, turns=1)

    consumer = frameworks.VerifiersRolloutConsumer(environment=object())
    resource = consumer.trajectory_resource(store, environment_id, who)
    rows = list(consumer.training_rows(store, environment_id, who))

    assert resource["kind"] == "Trajectory"
    assert resource["metadata"]["id"] == "trajectory-" + environment_id
    assert len(rows) == 1
    assert rows[0]["metadata"]["trajectory_id"] == resource["metadata"]["id"]
    assert rows[0]["metadata"]["record_id"]
    assert rows[0]["reward"] == 1.0


@pytest.mark.parametrize("installed", ["0.3.0", "0.4.0", "1.0.0"])
def test_verifiers_bridge_rejects_versions_outside_the_declared_extra(monkeypatch, installed):
    monkeypatch.setattr(frameworks, "version", lambda _package: installed)

    with pytest.raises(Unsupported, match="targets >=0.3.1,<0.4"):
        frameworks.VerifiersRolloutConsumer(environment=object())
