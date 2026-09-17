"""Human-readable inspection must group evidence by turn and respect participant visibility."""

import json

import pytest

from environment_harness import cli, presentation
from environment_harness.contracts import AgentSpec, ExperimentSpec, Principal, RunPolicy
from environment_harness.evaluation import compare
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.runner import run
from environment_harness.runtime import EnvironmentSession
from environment_harness.store import EvidenceStore


@pytest.fixture
def lineage(tmp_path):
    store = EvidenceStore(tmp_path)
    env = SyntheticEnvironment()
    session = EnvironmentSession(store, env)
    who = Principal(tenant="local", subject="local-researcher", role="researcher")
    spec = ExperimentSpec(
        environment=env.spec,
        participants=tuple(
            AgentSpec(id=p, implementation="synthetic@1", policy_version="1", checkpoint=True)
            for p in ("alice", "bob")
        ),
        scoring_versions=("control@1",),
        policy=RunPolicy(max_turns=20),
    )
    parent = session.create(spec, who)["id"]
    agents = {p: SyntheticAgent() for p in ("alice", "bob")}
    run(session, parent, who, agents, turns=3)
    lease = session.lease(parent, who, "test")
    checkpoint = session.checkpoint(parent, who, lease, exact_agents=True)
    session.release(parent, who, lease)
    child = session.branch(parent, who, checkpoint["id"], {"total": 20})["id"]
    run(session, parent, who, agents, turns=2)
    run(session, child, who, agents, turns=2)
    return store, session, who, parent, child, checkpoint["id"]


def timeline(store, who, environment, perspective=None):
    events = presentation.filter_events(store.replay(environment, who), perspective)
    return presentation.build_timeline(events, ("alice", "bob"))


def test_turns_group_observation_action_and_outcome(lineage):
    store, session, who, parent, child, checkpoint = lineage
    turns = timeline(store, who, parent)
    assert [turn["revision"] for turn in turns] == [0, 1, 2, 3, 4]
    first = turns[0]
    assert first["committed"]["revision"] == 1
    assert first["shared"] == {"total": 2}
    for participant in ("alice", "bob"):
        slots = first["participants"][participant]
        assert all(slots[slot] for slot in presentation.SLOTS)
        assert slots["executed"]["payload"]["action_id"] == slots["attempted"]["payload"]["action"]["operation_id"]
    assert "Session created with participants alice and bob." in presentation.render_timeline(turns)
    assert presentation.turn_heading(first).startswith("Revision 0 to 1")


def test_perspective_hides_other_participants(lineage):
    store, session, who, parent, child, checkpoint = lineage
    turns = timeline(store, who, parent, perspective="alice")
    assert all(turn["participants"]["bob"][slot] is None for turn in turns for slot in presentation.SLOTS)
    text = presentation.render_timeline(turns, perspective="alice")
    assert "synthetic-secret-alice" in text
    assert "synthetic-secret-bob" not in text
    assert "bob    not visible from this perspective" in text
    assert text.startswith("Revision 0 to 1")


def test_branch_collapses_inherited_history(lineage):
    store, session, who, parent, child, checkpoint = lineage
    turns = timeline(store, who, child)
    inherited = turns[0]["inherited"]
    assert inherited["count"] == 25 and inherited["parent"] == parent and inherited["checkpoint"] == checkpoint
    assert not any(event["kind"] == "history.inherited" for turn in turns for event in turn["other"])
    text = presentation.render_timeline(turns)
    assert f"Inherited 25 events from parent {parent[:12]} at checkpoint {checkpoint[:12]}." in text
    assert text.count("Inherited") == 1
    assert "interventions total 20" in text


def test_format_cost():
    assert presentation.format_cost(0) == "$0.00"
    assert presentation.format_cost(1) == "$0.000001"
    assert presentation.format_cost(1_500_000) == "$1.50"
    assert presentation.format_cost(123_456) == "$0.123456"


def test_title_and_short_id():
    assert presentation.title({"participants": ["alice", "bob"], "parent": None}) == "alice, bob (Original)"
    assert presentation.title({"participants": ["alice", "bob"], "parent": "x"}) == "alice, bob (Branch)"
    assert presentation.title({"environment": {"id": "synthetic-protocol"}, "parent": None}) == "synthetic-protocol (Original)"
    assert presentation.short_id("3b7dd9f540964c1a9f158f348324611f") == "3b7dd9f54096"


def test_cli_inspection_commands(lineage, tmp_path, monkeypatch, capsys):
    store, session, who, parent, child, checkpoint = lineage

    def main(*argv):
        monkeypatch.setattr("sys.argv", ["environment-harness", "--store", str(tmp_path), *argv])
        cli.main()
        return capsys.readouterr().out

    listing = main("list")
    assert parent in listing and child in listing and "alice, bob (Branch)" in listing
    assert json.loads(main("list", "--json"))[0]["id"] in (parent, child)
    shown = main("show", child)
    recorded = len(list(store.replay(child, who)))
    assert shown.startswith("alice, bob (Branch)\n") and f"Evidence holds {recorded} events across 2 turns." in shown
    assert "No score report recorded." in shown and "reports" in json.loads(main("show", child, "--json"))
    turns = json.loads(main("timeline", child, "--json"))
    assert turns[0]["revision"] == 3 and turns[0]["inherited"]["count"] == 25
    assert "synthetic-secret-alice" not in main("timeline", parent, "--participant", "bob")
    assert "Checkpoint" in main("timeline", parent, "--kind", "checkpoint")
    human = main("compare", parent, child)
    assert human.startswith("Compared 2 environments in 1 lineage.") and "alice, bob (Original)" in human
    assert "no score report" in human and "interventions total 20" in human
    assert json.loads(main("compare", parent, child, "--json")) == compare(store, [parent, child], who)
    lines = main("replay", parent).splitlines()
    assert len(lines) > 10 and all(json.loads(line)["seq"] == index + 1 for index, line in enumerate(lines))
