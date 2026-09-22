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
            AgentSpec(id=p, implementation="synthetic-agent@1", policy_version="1", checkpoint=True)
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
        assert (
            slots["executed"]["payload"]["action_id"]
            == slots["attempted"]["payload"]["action"]["operation_id"]
        )
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
    assert (
        inherited["count"] == 31 and inherited["parent"] == parent and inherited["checkpoint"] == checkpoint
    )
    assert not any(event["kind"] == "history.inherited" for turn in turns for event in turn["other"])
    text = presentation.render_timeline(turns)
    assert f"Inherited 31 events from parent {parent[:12]} at checkpoint {checkpoint[:12]}." in text
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
    assert (
        presentation.title({"environment": {"id": "synthetic-protocol"}, "parent": None})
        == "synthetic-protocol (Original)"
    )
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
    assert (
        shown.startswith("alice, bob (Branch)\n")
        and f"Evidence holds {recorded} events across 2 turns." in shown
    )
    assert "No score report recorded." in shown and "reports" in json.loads(main("show", child, "--json"))
    turns = json.loads(main("timeline", child, "--json"))
    assert turns[0]["revision"] == 3 and turns[0]["inherited"]["count"] == 31
    assert "synthetic-secret-alice" not in main("timeline", parent, "--participant", "bob")
    assert "Checkpoint" in main("timeline", parent, "--kind", "checkpoint")
    human = main("compare", parent, child)
    assert human.startswith("Compared 2 environments in 1 lineage.") and "alice, bob (Original)" in human
    assert "no score report" in human and "interventions total 20" in human
    assert json.loads(main("compare", parent, child, "--json")) == compare(store, [parent, child], who)
    lines = main("replay", parent).splitlines()
    assert len(lines) > 10 and all(json.loads(line)["seq"] == index + 1 for index, line in enumerate(lines))


def test_presentation_helpers_cover_sparse_and_failure_evidence():
    assert presentation.join_names([]) == ""
    assert presentation.join_names(["alice"]) == "alice"
    assert presentation.join_names(["alice", "bob", "carol"]) == "alice, bob and carol"
    assert presentation.format_time(None) == ""
    assert presentation.compact({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert presentation.compact(1.23456789) == "1.23457"
    assert presentation.scalars(None) == ""
    assert presentation.scalars("plain") == "plain"
    assert presentation.scalars({"short": 1, "long": "x" * 40}, limit=25).endswith("...")
    assert presentation.visible({"audience": ["*"]}, "alice")
    assert not presentation.visible({"audience": ["bob"]}, "alice")
    assert presentation.participant_of({"payload": {"participant": "alice"}}) == "alice"
    assert presentation.participant_of({"payload": {"action": {"participant": "bob"}}}) == "bob"
    assert presentation.participant_of({"payload": {}}) is None

    events = [
        {
            "seq": 1,
            "revision": 0,
            "kind": "session.created",
            "payload": {"experiment": {"participants": []}},
            "audience": [],
            "ingested": 1,
        },
        {
            "seq": 2,
            "revision": 0,
            "kind": "observation.delivered",
            "payload": {"payload": {"state": 1}},
            "audience": ["alice"],
            "ingested": 2,
        },
        {
            "seq": 3,
            "revision": 0,
            "kind": "observation.delivered",
            "payload": {"participant": "alice", "payload": {"state": 1}},
            "audience": ["alice"],
            "ingested": 3,
        },
        {
            "seq": 4,
            "revision": 0,
            "kind": "observation.delivered",
            "payload": {"participant": "alice", "payload": {"state": 2}},
            "audience": ["alice"],
            "ingested": 4,
        },
        {
            "seq": 5,
            "revision": 0,
            "kind": "action.attempted",
            "payload": {
                "action": {"participant": "alice", "payload": {"value": -1}},
                "receipt": {"status": "blocked", "reason": "policy"},
            },
            "audience": ["alice"],
            "ingested": 5,
        },
        {
            "seq": 6,
            "revision": 1,
            "kind": "action.executed",
            "payload": {
                "participant": "alice",
                "outcome": {"executed": True, "value": -1},
                "reward": 0.5,
                "reason": "synthetic",
            },
            "audience": ["alice"],
            "ingested": 6,
        },
        {
            "seq": 7,
            "revision": 1,
            "kind": "transition.committed",
            "payload": {"truncated": True},
            "audience": [],
            "ingested": 7,
        },
        {
            "seq": 8,
            "revision": 1,
            "kind": "checkpoint.committed",
            "payload": {"id": "checkpoint", "exact_agents": False},
            "audience": [],
            "ingested": 8,
        },
        {
            "seq": 9,
            "revision": 1,
            "kind": "artifact",
            "payload": {"id": "artifact"},
            "audience": [],
            "ingested": 9,
        },
        {
            "seq": 10,
            "revision": 1,
            "kind": "report",
            "payload": {"revision": 2},
            "audience": [],
            "ingested": 10,
        },
    ]
    turns = presentation.build_timeline(events, ("alice", "bob"))
    rendered = presentation.render_timeline(turns, verbose=True, perspective="alice")
    assert "Session created." in rendered
    assert "blocked: policy" in rendered
    assert "reward 0.50 (synthetic)" in rendered
    assert "truncated" in rendered
    assert "Checkpoint checkpoint saved without exact agent state." in rendered
    assert "Artifact artifact recorded." in rendered
    assert "Score report revision 2 recorded." in rendered
    assert "No recorded events." == presentation.render_timeline([])
    assert presentation.participant_line("bob", presentation._empty_slots(), "alice").endswith(
        "not visible from this perspective"
    )


def test_sparse_environment_reports_and_comparison_rendering():
    assert presentation.render_list([]) == "No environments recorded."
    report = {
        "revision": 2,
        "report": {
            "scorer": "control",
            "version": "1",
            "evidence_cursor": 3,
            "metrics": {"score": 1},
            "findings": [{"rule": "one"}, {"rule": "two"}],
        },
    }
    assert "2 findings" in presentation.report_sentence(report)
    item = {
        "id": "environment",
        "status": "completed",
        "revision": 1,
        "lineage": "lineage",
        "parent": "parent",
        "environment": {
            "id": "synthetic",
            "implementation": "synthetic@1",
            "scheduling": "sequential",
            "capabilities": {"checkpoint": True, "resume": False},
        },
        "experiment": {"purpose": "evaluation", "split": "heldout"},
        "spent_micros": 1,
        "reserved_micros": 2,
    }
    environment = presentation.render_environment(item, [report], [])
    assert "Branched from parent" in environment
    assert "Capabilities: checkpoint." in environment
    assert "Score report revision 2" in environment
    grouped = {
        "environments": [
            {
                "environment": "env",
                "participants": ["alice"],
                "lineage": "lineage",
                "status": "completed",
                "revision": 1,
                "interventions": {"total": 3},
            }
        ],
        "metric_groups": [
            {
                "id": "group",
                "metric": "score",
                "scorer": "control",
                "version": "1",
                "kind": "deterministic",
                "definition": {"unit": "points"},
                "summary": None,
                "values": [{"environment": "env", "value": 1}],
                "reported_environments": 1,
                "selected_environments": 1,
                "missing_environments": 0,
                "incomplete_environments": 0,
            }
        ],
        "warnings": ["synthetic warning"],
        "uncertainty": "uncertain",
        "design": "controlled",
    }
    rendered = presentation.render_comparison(grouped)
    assert "Raw values only: env=1" in rendered
    assert "synthetic warning" in rendered and rendered.endswith("controlled")
    legacy = presentation.render_comparison(
        {"environments": [], "metrics": {"score": {"mean_of_lineage_means": 1}}}
    )
    assert "score  mean of lineage means 1" in legacy


def test_presentation_optional_details_are_independent():
    assert presentation.inherited_sentence(
        {
            "count": 1,
            "parent": "parent",
            "records": [{"complete": False}],
        }
    ).endswith("some records need more event pages.")
    for kind, expected in (
        ("action.attempted", "attempted"),
        ("action.executed", "executed"),
        ("transition.committed", "State revision 1 committed."),
        ("operation.intent", "alice prepared synthetic.inspect-total as operation inspect."),
        ("operation.receipt", "Operation inspect completed: meets threshold True."),
    ):
        event = {
            "kind": kind,
            "revision": 1,
            "payload": {
                "participant": "alice",
                "action": {"participant": "alice", "payload": {}},
                "receipt": {},
                "outcome": "complete",
            },
        }
        if kind == "operation.intent":
            event["payload"] = {
                "id": "inspect",
                "participant": "alice",
                "request": {"operation": "synthetic.inspect-total"},
            }
        if kind == "operation.receipt":
            event["payload"] = {
                "id": "inspect",
                "receipt": {"operation_id": "session:inspect", "meets_threshold": True},
            }
        assert expected in presentation.describe(event)
    assert presentation.cell_text({"payload": {"outcome": "complete"}}, "executed") == ("executed complete")
    assert presentation.participant_line("alice", presentation._empty_slots()) == (
        "alice  no events in this turn"
    )
    assert presentation.report_sentence({"revision": 1, "report": {}}).endswith(".")

    sparse = {
        "id": "environment",
        "status": "running",
        "revision": 0,
        "lineage": "environment",
        "environment": {"id": "synthetic"},
    }
    text = presentation.render_environment(sparse, [], [])
    assert "No score report recorded." in text
    assert "Capabilities:" not in text and "scheduling" not in text and "purpose" not in text

    branch_event = {
        "seq": 1,
        "revision": 0,
        "kind": "session.branched",
        "payload": {"parent": "parent", "checkpoint": "checkpoint"},
        "audience": [],
        "ingested": 1,
    }
    turns = presentation.build_timeline([branch_event])
    assert turns[0]["other"] == [branch_event]
