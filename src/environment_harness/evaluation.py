"""Evidence projections and lineage-aware comparisons. No domain grading rules."""

import json
import math
import statistics

from .errors import Forbidden, Unsupported
from .presentation import SLOTS, build_timeline, next_revision
from .store import digest


def _bounded_points(points, max_points):
    """Bound a series while retaining the first, last, and bucket extrema."""
    if len(points) <= max_points:
        return points, False
    interior = points[1:-1]
    bucket_count = max(1, (max_points - 2) // 2)
    bucket_size = math.ceil(len(interior) / bucket_count)
    selected = [points[0]]
    for offset in range(0, len(interior), bucket_size):
        bucket = interior[offset : offset + bucket_size]
        extrema = {
            min(range(len(bucket)), key=lambda index: bucket[index]["value"]),
            max(range(len(bucket)), key=lambda index: bucket[index]["value"]),
        }
        selected.extend(bucket[index] for index in sorted(extrema))
    selected.append(points[-1])
    return selected, True


def turn_series(store, environment, access, *, start_turn=1, end_turn=None, max_points=300):
    """Project bounded turn-level evidence series for comparison and visualization."""
    with store.transaction() as db:
        row = store.environment(db, environment, access, "evidence.read.full")
        participants = list(json.loads(row["participants"]))
    events = list(store.replay(environment, access))
    turns = [
        turn
        for turn in build_timeline(events, participants)
        if any(slots[slot] for slots in turn["participants"].values() for slot in SLOTS)
    ]
    total_turns = len(turns)
    first_turn = start_turn
    final_turn = total_turns if end_turn is None else end_turn
    turn_by_revision = {}
    turn_by_starting_revision = {}
    revision_by_turn = {}
    for index, turn in enumerate(turns, 1):
        turn_by_starting_revision[turn["revision"]] = index
        revision = next_revision(turn)
        if revision is not None:
            turn_by_revision[revision] = index
            revision_by_turn[index] = revision

    projected = {}

    def append(series_id, label, kind, unit, participant, turn, revision, value):
        if turn is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if not math.isfinite(value):
            return
        series = projected.setdefault(
            series_id,
            {
                "id": series_id,
                "label": label,
                "kind": kind,
                "unit": unit,
                "participant": participant,
                "points": [],
            },
        )
        series["points"].append({"turn": turn, "revision": revision, "value": value})

    rewards_by_turn = {}
    activity = {
        index: {"observed": 0, "attempted": 0, "executed": 0, "blocked": 0}
        for index in range(1, total_turns + 1)
    }
    for event in events:
        kind, payload, revision = event["kind"], event.get("payload") or {}, event["revision"]
        if kind == "action.executed":
            turn = turn_by_revision.get(revision)
            reward = payload.get("reward")
            if turn is not None and isinstance(reward, (int, float)) and not isinstance(reward, bool):
                rewards_by_turn[turn] = rewards_by_turn.get(turn, 0) + reward
            if turn is not None:
                activity[turn]["executed"] += 1
        elif kind in ("observation.delivered", "action.attempted"):
            turn = turn_by_starting_revision.get(revision)
            if turn is not None:
                activity[turn]["observed" if kind == "observation.delivered" else "attempted"] += 1
                if kind == "action.attempted" and (payload.get("receipt") or {}).get("status") == "blocked":
                    activity[turn]["blocked"] += 1
        if event.get("audience") == ["*"] and kind not in ("transition.committed", "action.executed"):
            turn = turn_by_revision.get(revision)
            for field, value in payload.items():
                append(
                    f"signal:{kind}:{field}",
                    f"{kind} · {field.replace('_', ' ').title()}",
                    "signal",
                    None,
                    None,
                    turn,
                    revision,
                    value,
                )

    cumulative_reward = 0
    cumulative_executed = 0
    for turn in range(1, total_turns + 1):
        revision = revision_by_turn.get(turn, turns[turn - 1]["revision"])
        cumulative_reward += rewards_by_turn.get(turn, 0)
        cumulative_executed += activity[turn]["executed"]
        append(
            "reward:cumulative",
            "Cumulative Reward",
            "reward",
            "reward",
            None,
            turn,
            revision,
            cumulative_reward,
        )
        append(
            "activity:executed:cumulative",
            "Executed Actions",
            "activity",
            "count",
            None,
            turn,
            revision,
            cumulative_executed,
        )

    series = []
    for item in projected.values():
        points = [point for point in item.pop("points") if first_turn <= point["turn"] <= final_turn]
        bounded, downsampled = _bounded_points(points, max_points)
        series.append(item | {"source_points": len(points), "downsampled": downsampled, "points": bounded})
    return {
        "environment": environment,
        "total_turns": total_turns,
        "range": {"start_turn": first_turn, "end_turn": final_turn},
        "max_points": max_points,
        "series": sorted(series, key=lambda item: item["id"]),
    }


def rollouts(store, environment, access, *, require_token_ids=False, require_logprobs=False):
    with store.transaction() as db:
        row = store.environment(db, environment, access, "evidence.read.full")
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
    for event in store.replay(environment, access):
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
                r
                for r in store.reports(environment, access)
                if outcome["participant"] in r["report"]["rewards"]
            ],
            "outcomes_pending": status == "outcomes_pending",
            "token_ids": None,
            "logprobs": None,
        }


def compare(store, environments, access):
    groups, cohorts, records = {}, {}, []
    for environment in dict.fromkeys(environments):
        with store.transaction() as db:
            row = store.environment(db, environment, access, "evidence.read.full")
            manifest = json.loads(row["manifest"])
            cohort = {k: manifest[k] for k in ("environment", "participants", "purpose", "split", "policy")}
            cohort["operations"] = manifest.get("operations", [])
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
        reports = store.reports(environment, access)
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
