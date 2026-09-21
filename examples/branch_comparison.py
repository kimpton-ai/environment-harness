"""A complete offline experiment using only the public synthetic fixture."""

import argparse
import json
from pathlib import Path

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.contracts import RunPolicy, ScoreReport
from environment_harness.evaluation import compare
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.runner import run


def experiment(directory):
    store = EvidenceStore(directory)
    session = EnvironmentSession(store, SyntheticEnvironment())
    who = Principal(tenant="local", subject="demo-researcher", role="researcher")
    spec = ExperimentSpec(
        environment=session.environment.spec,
        participants=tuple(
            AgentSpec(id=p, implementation="synthetic-agent@1", policy_version="1", checkpoint=True)
            for p in ("alice", "bob")
        ),
        policy=RunPolicy(max_turns=5),
        scoring_versions=("synthetic-total@1",),
    )

    def advance(environment, turns):
        return run(session, environment, who, {p: SyntheticAgent() for p in ("alice", "bob")}, turns=turns)

    parent = session.create(spec, who)["id"]
    advance(parent, 3)
    print("Original session: two synthetic agents, three turns, shared counter total 6.")
    lease = session.lease(parent, who, "demo-checkpoint")
    try:
        checkpoint = session.checkpoint(parent, who, lease, exact_agents=True)
    finally:
        session.release(parent, who, lease)
    child = session.branch(parent, who, checkpoint["id"], {"total": 20})["id"]
    print("Checkpoint saved. The branched session starts at total 20; the original stays at 6.")

    observations = {p: session.observe(parent, who, p)["payload"] for p in ("alice", "bob")}
    for environment in (parent, child):
        advance(environment, 2)
        total = session.observe(environment, who, "alice")["payload"]["total"]
        cursor = store.verify(environment, who)["events"]
        store.report(
            environment,
            who,
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
            ),
        )
        with (Path(directory) / f"{environment}.jsonl").open("w") as output:
            for event in store.replay(environment, who):
                output.write(json.dumps(event) + "\n")

    result = {
        "synthetic": True,
        "parent": parent,
        "branch": child,
        "checkpoint": checkpoint["id"],
        "observations_at_checkpoint": observations,
        "totals": {w: session.observe(w, who, "alice")["payload"]["total"] for w in (parent, child)},
        "statuses": {w: session.get(w, who)["status"] for w in (parent, child)},
        "comparison": compare(store, [parent, child], who),
        "evidence": {w: store.verify(w, who) for w in (parent, child)},
    }
    (Path(directory) / "demo.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Both sessions advanced two more turns. Original total: 10. Branched total: 24.")
    print(f"Original session: {parent}\nBranched session: {child}\nReport: {Path(directory) / 'demo.json'}")
    print(
        "The environment sessions share one lineage. Their difference is not independent statistical evidence."
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".local/branch-demo")
    args = parser.parse_args()
    experiment(args.store)
