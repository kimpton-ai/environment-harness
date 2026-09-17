"""Evidence projections and lineage-aware comparisons. No domain grading rules."""

import json
import math
import statistics

from .errors import Forbidden, Unsupported


def rollouts(store, environment, who, *, require_token_ids=False, require_logprobs=False):
    with store.transaction() as db:
        row = store.environment(db, environment, who, ("researcher", "scorer"))
        manifest = json.loads(row["manifest"])
        if (
            manifest["purpose"] != "training"
            or manifest["split"] != "training"
            or "training" not in manifest["environment"]["purposes"]
        ):
            raise Forbidden("training export denied by experiment entitlement")
        caps = manifest["environment"]["capabilities"]
        if require_token_ids and not caps["token_ids"] or require_logprobs and not caps["logprobs"]:
            raise Unsupported("requested inference detail was not captured")
        lineage, parent, status = row["lineage"], row["parent"], row["status"]
    # Stream from the evidence store. Join each action individually to keep campaign memory bounded.
    for event in store.replay(environment, who):
        if event["kind"] != "action.executed":
            continue
        outcome = event["payload"]
        with store.transaction() as db:
            action = db.execute(
                "SELECT * FROM actions WHERE environment=? AND id=?", (environment, outcome["action_id"])
            ).fetchone()
            request = json.loads(action["request"])
            obs = json.loads(
                db.execute(
                    "SELECT body FROM observations WHERE environment=? AND id=?", (environment, request["observation_id"])
                ).fetchone()[0]
            )
        if require_token_ids or require_logprobs:
            raise Unsupported("this export has no action-correlated token records")
        yield {
            "schema": "environment-rollout.v1",
            "environment": environment,
            "lineage": lineage,
            "parent": parent,
            "participant": outcome["participant"],
            "policy_version": outcome["policy_version"],
            "observation": obs,
            "action": request,
            "reward": outcome["reward"],
            "terminated": outcome["terminated"],
            "truncated": outcome["truncated"],
            "reason": outcome["reason"],
            "delayed_rewards": [
                r for r in store.reports(environment, who) if outcome["participant"] in r["report"]["rewards"]
            ],
            "outcomes_pending": status == "outcomes_pending",
            "token_ids": None,
            "logprobs": None,
        }


def compare(store, environments, who):
    groups = {}
    records = []
    for environment in environments:
        with store.transaction() as db:
            row = store.environment(db, environment, who, ("researcher", "scorer"))
            record = {
                "environment": environment,
                "lineage": row["lineage"],
                "parent": row["parent"],
                "status": row["status"],
                "revision": row["revision"],
                "participants": list(json.loads(row["participants"])),
                "cost_micros": row["spent"],
                "interventions": json.loads(row["manifest"])["interventions"],
            }
        reports = store.reports(environment, who)
        record["latest_report"] = reports[-1] if reports else None
        groups.setdefault(record["lineage"], []).append(record)
        records.append(record)
    metrics = {}
    for lineage, members in groups.items():
        local = {}
        for member in members:
            report = member["latest_report"]
            if report:
                for key, value in report["report"]["metrics"].items():
                    if (
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(value)
                    ):
                        local.setdefault(key, []).append(value)
        for key, values in local.items():
            metrics.setdefault(key, []).append(statistics.mean(values))
    summary = {
        key: {
            "mean_of_lineage_means": statistics.mean(values),
            "independent_lineages": len(values),
            "standard_error": statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None,
        }
        for key, values in metrics.items()
    }
    return {
        "environments": records,
        "metrics": summary,
        "uncertainty": "Branches and turns share a lineage. One lineage cannot establish between-environment uncertainty.",
        "design": "Interventions are declared. Causal identification still depends on the experiment design.",
    }
