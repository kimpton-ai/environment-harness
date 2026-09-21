"""Evidence projections and lineage-aware comparisons. No domain grading rules."""

import json
import math
import statistics

from .errors import Forbidden, Unsupported
from .store import digest


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
                    "SELECT body FROM observations WHERE environment=? AND id=?",
                    (environment, request["observation_id"]),
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
    groups, cohorts, records = {}, {}, []
    for environment in dict.fromkeys(environments):
        with store.transaction() as db:
            row = store.environment(db, environment, who, ("researcher", "scorer"))
            manifest = json.loads(row["manifest"])
            cohort = {k: manifest[k] for k in ("environment", "participants", "purpose", "split", "policy")}
            cohort["motor"] = manifest.get("motor")
            cohort_id = digest(cohort)
            record = {
                "environment": environment,
                "lineage": row["lineage"],
                "parent": row["parent"],
                "status": row["status"],
                "revision": row["revision"],
                "participants": list(json.loads(row["participants"])),
                "cost_micros": row["spent"],
                "interventions": manifest["interventions"],
                "cohort": cohort_id,
            }
        cohorts.setdefault(cohort_id, []).append(record)
        reports = store.reports(environment, who)
        record["latest_report"] = reports[-1] if reports else None
        selected = {}
        for envelope in reports:
            report = envelope["report"]
            selected[(report["scorer"], report["version"], report["kind"])] = envelope
        record["selected_reports"] = [selected[key] for key in sorted(selected)]
        records.append(record)
        for envelope in record["selected_reports"]:
            report = envelope["report"]
            for metric, value in report["metrics"].items():
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    continue
                definition = report.get("metric_definitions", {}).get(metric)
                identity = {
                    "cohort": cohort_id,
                    "scorer": report["scorer"],
                    "version": report["version"],
                    "kind": report["kind"],
                    "metric": metric,
                    "definition": definition,
                }
                group_id = digest(identity)
                group = groups.setdefault(
                    group_id,
                    identity
                    | {
                        "id": group_id,
                        "experiment": cohort,
                        "values": [],
                        "summary": None,
                    },
                )
                group["values"].append(
                    {
                        "environment": environment,
                        "lineage": record["lineage"],
                        "status": record["status"],
                        "report_revision": envelope["revision"],
                        "report_hash": envelope["hash"],
                        "value": value,
                    }
                )
    warnings, names = [], {}
    for group in groups.values():
        names.setdefault(group["metric"], []).append(group)
        eligible = cohorts[group["cohort"]]
        group["selected_environments"] = len(eligible)
        group["reported_environments"] = len(group["values"])
        group["missing_environments"] = len(eligible) - len(group["values"])
        group["incomplete_environments"] = sum(r["status"] != "completed" for r in eligible)
        if group["definition"] is None:
            warnings.append(
                f"{group['scorer']}@{group['version']} {group['metric']}: "
                "metric definition missing; raw values only."
            )
            continue
        lineages = {}
        for value in group["values"]:
            lineages.setdefault(value["lineage"], []).append(value["value"])
        means = [statistics.mean(values) for values in lineages.values()]
        group["summary"] = {
            "mean_of_lineage_means": statistics.mean(means),
            "independent_lineages": len(means),
            "standard_error": statistics.stdev(means) / math.sqrt(len(means)) if len(means) > 1 else None,
        }
    metrics = {}
    for metric, matches in names.items():
        if len(matches) == 1 and matches[0]["summary"] is not None:
            metrics[metric] = matches[0]["summary"]
        elif len(matches) > 1:
            warnings.append(
                f"{metric}: incompatible score groups are shown separately; no combined statistic."
            )
    return {
        "environments": records,
        "metrics": metrics,
        "metric_groups": sorted(
            groups.values(), key=lambda g: (g["metric"], g["scorer"], g["version"], g["id"])
        ),
        "warnings": warnings,
        "uncertainty": "Branches and turns share a lineage. One lineage cannot establish between-environment uncertainty.",
        "design": "Interventions are declared. Causal identification still depends on the experiment design.",
    }
