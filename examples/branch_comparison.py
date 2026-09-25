"""A complete offline experiment using only the public synthetic fixture."""

import argparse
import json
from pathlib import Path

from environment_harness import EnvironmentHarness, EvidenceStore, Scenario
from environment_harness.contracts import BranchRequest, RunPolicy, ScoreReport
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment


def first_three_turns(control, agents, *, turns):
    """Stop after three turns so the example can checkpoint mid-budget."""

    return control.advance(agents, turns=3)


def experiment(directory):
    store = EvidenceStore(directory)
    harness = EnvironmentHarness(
        store,
        environment=SyntheticEnvironment,
        agents={"alice": SyntheticAgent, "bob": SyntheticAgent},
        scoring_versions=("synthetic-total@1",),
        policy=RunPolicy(max_turns=5),
        session_runner=first_three_turns,
    )
    parent = harness.run(Scenario(id="branch-demo", input={}), turns=5)
    print("Original session: two synthetic agents, three turns, shared counter total 6.")

    checkpoint = parent.checkpoint(exact_agents=True)
    observations = {p: parent.observation(p)["payload"] for p in ("alice", "bob")}
    child = parent.branch(BranchRequest(checkpoint=checkpoint["id"], interventions={"total": 20}))
    print("Checkpoint saved. The branched session starts at total 20; the original stays at 6.")

    for session in (parent, child):
        session.advance(turns=2)
        total = session.observation("alice")["payload"]["total"]
        cursor = session.verify()["events"]
        session.report(
            ScoreReport(
                scorer="synthetic-total",
                version="1",
                kind="deterministic",
                evidence_cursor=cursor,
                metrics={"synthetic_total": total},
                metric_definitions={
                    "synthetic_total": {"id": "synthetic-total", "version": "1", "unit": "count"}
                },
                uncertainty="Protocol fixture only. This is not a model-performance or safety measure.",
                provenance={"synthetic": True, "source": "examples/branch_comparison.py"},
            )
        )
        with (Path(directory) / f"{session.id}.jsonl").open("w") as output:
            for event in session.replay():
                output.write(json.dumps(event) + "\n")

    result = {
        "synthetic": True,
        "parent": parent.id,
        "branch": child.id,
        "checkpoint": checkpoint["id"],
        "observations_at_checkpoint": observations,
        "totals": {s.id: s.observation("alice")["payload"]["total"] for s in (parent, child)},
        "statuses": {s.id: s.record()["status"] for s in (parent, child)},
        "comparison": harness.compare([parent.id, child.id]),
        "evidence": {s.id: s.verify() for s in (parent, child)},
    }
    (Path(directory) / "demo.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Both sessions advanced two more turns. Original total: 10. Branched total: 24.")
    print(
        f"Original session: {parent.id}\nBranched session: {child.id}\n"
        f"Report: {Path(directory) / 'demo.json'}"
    )
    print(
        "The environment sessions share one lineage. Their difference is not independent "
        "statistical evidence."
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".local/branch-demo")
    args = parser.parse_args()
    experiment(args.store)
