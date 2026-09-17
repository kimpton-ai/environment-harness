"""Human-readable views of recorded evidence.

These are pure functions over the event and environment dictionaries that
``EvidenceStore.replay`` and ``EnvironmentSession.get`` already return. They
perform no I/O, so the command line, tests and the browser viewer
(``packages/typescript/src/timeline.ts``, which mirrors the grouping rules and
field names) all present the same evidence the same way.

A turn is keyed by the state revision an action was taken from. Observations
and attempted actions carry that revision. The transition that commits the
next revision, the executed outcomes and the shared-state broadcast that
follows it carry ``revision + 1``, so they are assigned to the previous turn.
Environments whose outcomes commit later than the next revision fall back to
their own revision; every event stays visible, and ``--json`` exposes the raw
sequence numbers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

SLOTS = ("observation", "attempted", "executed")
SLOT_KINDS = {"observation.delivered": "observation", "action.attempted": "attempted", "action.executed": "executed"}
OUTCOME_KINDS = {"transition.committed", "action.executed"}
SHORT = 12


def short_id(value) -> str:
    return str(value)[:SHORT]


def join_names(names) -> str:
    names = [str(name) for name in names]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def title(environment) -> str:
    participants = environment.get("participants") or []
    role = "Branch" if environment.get("parent") else "Original"
    if participants:
        return f"{', '.join(participants)} ({role})"
    spec = environment.get("environment") or {}
    return f"{spec.get('id', short_id(environment.get('id', '')))} ({role})"


def format_cost(micros) -> str:
    text = f"{(micros or 0) / 1e6:.6f}"
    whole, fraction = text.split(".")
    fraction = fraction.rstrip("0")
    return f"${whole}.{fraction.ljust(2, '0')}"


def format_time(seconds) -> str:
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def compact(value) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


LONG_TEXT = 24


def scalars(mapping, limit=100) -> str:
    """Summarise a mapping on one line. Short values lead; long text is quoted, trimmed and placed last."""
    if not isinstance(mapping, dict) or not mapping:
        return compact(mapping) if mapping not in (None, {}) else ""
    short, long = [], []
    for key, value in mapping.items():
        if isinstance(value, str) and len(value) > LONG_TEXT:
            long.append(f'{key} "{value[:LONG_TEXT].rstrip()}..."')
        else:
            short.append(f"{key} {compact(value)}")
    text = ", ".join(short + long)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def visible(event, perspective=None) -> bool:
    if not perspective:
        return True
    audience = event.get("audience") or []
    return "*" in audience or perspective in audience


def filter_events(events, perspective=None, kind=None):
    return [event for event in events if visible(event, perspective) and (not kind or kind in event["kind"])]


def participant_of(event):
    payload = event.get("payload") or {}
    if isinstance(payload.get("participant"), str):
        return payload["participant"]
    action = payload.get("action")
    if isinstance(action, dict) and isinstance(action.get("participant"), str):
        return action["participant"]
    return None


def event_time(event):
    return event.get("event_time") if event.get("event_time") is not None else event.get("ingested")


def _empty_slots():
    return {slot: None for slot in SLOTS}


def _turn(turns, revision, participants):
    if revision not in turns:
        turns[revision] = {
            "revision": revision,
            "committed": None,
            "started": None,
            "shared": {},
            "participants": {participant: _empty_slots() for participant in participants},
            "inherited": None,
            "other": [],
            "seqs": [],
        }
    return turns[revision]


def build_timeline(events, participants=()):
    turns = {}
    committed = set()
    for event in sorted(events, key=lambda row: row["seq"]):
        kind, revision, payload = event["kind"], event["revision"], event.get("payload") or {}
        if kind == "history.inherited":
            turn = _turn(turns, revision, participants)
            record = turn["inherited"] or {
                "count": 0,
                "parent": payload.get("environment"),
                "checkpoint": None,
                "first_seq": event["seq"],
                "last_seq": event["seq"],
            }
            record["count"] += 1
            record["last_seq"] = event["seq"]
            turn["inherited"] = record
            continue
        shared = event.get("audience") == ["*"] and kind not in SLOT_KINDS and kind not in OUTCOME_KINDS
        if kind in OUTCOME_KINDS:
            if kind == "transition.committed":
                committed.add(revision)
            key = revision - 1 if revision > 0 else revision
        elif shared and (revision in committed or (revision - 1 in turns and revision not in turns)):
            # A broadcast that follows a commit belongs to the turn that produced it. When the
            # commit itself is hidden from this perspective, the surrounding turns still tell us.
            key = revision - 1
        else:
            key = revision
        turn = _turn(turns, key, participants)
        turn["seqs"].append(event["seq"])
        when = event_time(event)
        if when is not None and (turn["started"] is None or when < turn["started"]):
            turn["started"] = when
        if kind == "transition.committed":
            turn["committed"] = {
                "revision": revision,
                "seq": event["seq"],
                "state_hash": payload.get("state_hash"),
                "terminated": bool(payload.get("terminated")),
                "truncated": bool(payload.get("truncated")),
            }
        elif kind in SLOT_KINDS:
            participant = participant_of(event)
            slot = SLOT_KINDS[kind]
            if participant is None:
                turn["other"].append(event)
                continue
            slots = turn["participants"].setdefault(participant, _empty_slots())
            if slots[slot] is None:
                slots[slot] = event
            else:
                turn["other"].append(event)
        elif shared:
            turn["shared"].update({key: value for key, value in payload.items() if not isinstance(value, (dict, list))})
        else:
            if kind == "session.branched":
                branch_turn = _turn(turns, revision, participants)
                record = branch_turn["inherited"]
                if record is not None:
                    record["checkpoint"] = payload.get("checkpoint")
                    record["parent"] = record["parent"] or payload.get("parent")
            turn["other"].append(event)
    return [turns[key] for key in sorted(turns)]


def inherited_sentence(record) -> str:
    text = f"Inherited {record['count']} events from parent {short_id(record['parent'])}"
    if record.get("checkpoint"):
        text += f" at checkpoint {short_id(record['checkpoint'])}"
    return text + "."


def describe(event) -> str:
    kind, payload = event["kind"], event.get("payload") or {}
    if kind == "session.created":
        participants = [p["id"] for p in (payload.get("experiment") or {}).get("participants", []) if "id" in p]
        return f"Session created with participants {join_names(participants)}." if participants else "Session created."
    if kind == "session.branched":
        text = f"Branched from {short_id(payload.get('parent'))} at checkpoint {short_id(payload.get('checkpoint'))}"
        interventions = payload.get("interventions") or {}
        return text + (f" with interventions {scalars(interventions)}." if interventions else ".")
    if kind == "checkpoint.committed":
        exact = "with" if payload.get("exact_agents") else "without"
        return f"Checkpoint {short_id(payload.get('id'))} saved {exact} exact agent state."
    if kind == "report":
        return f"Score report revision {payload.get('revision')} recorded."
    if kind == "artifact":
        return f"Artifact {payload.get('id')} recorded."
    if kind == "observation.delivered":
        return f"{participant_of(event)} observed {scalars(payload.get('payload'))}."
    if kind == "action.attempted":
        return f"{participant_of(event)} attempted {cell_text(event, 'attempted')[10:]}."
    if kind == "action.executed":
        return f"{participant_of(event)} executed {cell_text(event, 'executed')[9:]}."
    if kind == "transition.committed":
        return f"State revision {event['revision']} committed."
    summary = scalars(payload)
    return f"{kind} recorded: {summary}." if summary else f"{kind} recorded."


def cell_text(event, slot) -> str:
    payload = event.get("payload") or {}
    if slot == "observation":
        return f"observed {scalars(payload.get('payload'))}".rstrip()
    if slot == "attempted":
        action = payload.get("action") or {}
        receipt = payload.get("receipt") or {}
        status = receipt.get("status", "submitted")
        if receipt.get("reason"):
            status += f": {receipt['reason']}"
        return f"attempted {scalars(action.get('payload'))} ({status})"
    outcome = payload.get("outcome")
    if isinstance(outcome, dict):
        outcome = {key: value for key, value in outcome.items() if key != "executed" or value is not True}
    text = f"executed {scalars(outcome)}".rstrip()
    if isinstance(payload.get("reward"), (int, float)):
        text += f", reward {payload['reward']:.2f}"
    if payload.get("reason"):
        text += f" ({payload['reason']})"
    return text


MISSING = {"observation": "no observation", "attempted": "no action yet", "executed": "not executed"}


def next_revision(turn):
    """The revision this turn committed, inferred from executed outcomes when the commit is hidden."""
    if turn["committed"]:
        return turn["committed"]["revision"]
    executed = [slots["executed"] for slots in turn["participants"].values() if slots["executed"]]
    return executed[0]["revision"] if executed else None


def turn_heading(turn) -> str:
    committed = turn["committed"]
    target = next_revision(turn)
    label = f"Revision {turn['revision']} to {target}" if target is not None else f"Revision {turn['revision']} (open)"
    parts = [label, format_time(turn["started"])]
    if committed and (committed["terminated"] or committed["truncated"]):
        parts.append("terminated" if committed["terminated"] else "truncated")
    if turn["shared"]:
        parts.append(scalars(turn["shared"]))
    return "  ".join(part for part in parts if part)


def participant_line(name, slots, perspective=None) -> str:
    if perspective and name != perspective and all(slots[slot] is None for slot in SLOTS):
        return f"{name}  not visible from this perspective"
    cells = [cell_text(slots[slot], slot) if slots[slot] else MISSING[slot] for slot in SLOTS]
    if slots["observation"] is None and slots["attempted"] is None and slots["executed"] is None:
        cells = ["no events in this turn"]
    return f"{name}  " + "  |  ".join(cells)


def render_timeline(turns, verbose=False, perspective=None) -> str:
    if not turns:
        return "No recorded events."
    lines = []
    for turn in turns:
        lines.append(turn_heading(turn))
        first_participant_seq = min(
            (slots[slot]["seq"] for slots in turn["participants"].values() for slot in SLOTS if slots[slot]),
            default=None,
        )
        before = [e for e in turn["other"] if first_participant_seq is None or e["seq"] < first_participant_seq]
        after = [e for e in turn["other"] if first_participant_seq is not None and e["seq"] >= first_participant_seq]
        if turn["inherited"]:
            lines.append("  " + inherited_sentence(turn["inherited"]))
        lines.extend("  " + describe(event) for event in before)
        width = max((len(name) for name in turn["participants"]), default=0)
        active = any(slots[slot] for slots in turn["participants"].values() for slot in SLOTS)
        for name, slots in turn["participants"].items() if active else ():
            line = participant_line(name, slots, perspective)
            lines.append("  " + line.replace(f"{name}  ", f"{name.ljust(width)}  ", 1))
            if verbose:
                for slot in SLOTS:
                    if slots[slot]:
                        body = json.dumps(slots[slot]["payload"], indent=2, sort_keys=True)
                        lines.append(f"    {slot} #{slots[slot]['seq']}")
                        lines.extend("      " + row for row in body.splitlines())
        lines.extend("  " + describe(event) for event in after)
    return "\n".join(lines)


def render_list(environments) -> str:
    if not environments:
        return "No environments recorded."
    rows = [(item["id"], title(item), item["status"], f"revision {item['revision']}") for item in environments]
    widths = [max(len(row[column]) for row in rows) for column in range(3)]
    return "\n".join(
        "  ".join([row[0].ljust(widths[0]), row[1].ljust(widths[1]), row[2].ljust(widths[2]), row[3]]) for row in rows
    )


def report_sentence(record) -> str:
    report = record.get("report") or {}
    metrics = scalars(report.get("metrics") or {})
    scorer = f"{report.get('scorer')}@{report.get('version')}" if report.get("scorer") else "unknown scorer"
    text = f"Score report revision {record.get('revision')} by {scorer} at evidence cursor {report.get('evidence_cursor')}"
    text += f": {metrics}." if metrics else "."
    findings = report.get("findings") or []
    if findings:
        text += f" {len(findings)} finding{'s' if len(findings) != 1 else ''}."
    return text


def render_environment(item, reports, turns) -> str:
    spec = item.get("environment") or {}
    experiment = item.get("experiment") or {}
    lines = [title(item), f"Environment {item['id']}"]
    status = f"Status {item['status']} at revision {item['revision']}. Lineage {short_id(item['lineage'])}."
    branched = next((e for turn in turns for e in turn["other"] if e["kind"] == "session.branched"), None)
    if branched:
        status += " " + describe(branched)
    elif item.get("parent"):
        status += f" Branched from {short_id(item['parent'])}."
    lines.append(status)
    detail = f"Environment {spec.get('implementation', spec.get('id', 'unknown'))}"
    if spec.get("scheduling"):
        detail += f" with {spec['scheduling']} scheduling"
    if experiment.get("purpose"):
        detail += f", {experiment['purpose']} purpose and {experiment.get('split', 'unspecified')} split"
    lines.append(detail + ".")
    capabilities = [name for name, enabled in (spec.get("capabilities") or {}).items() if enabled]
    if capabilities:
        lines.append(f"Capabilities: {', '.join(capabilities)}.")
    lines.append(f"Spent {format_cost(item.get('spent_micros'))}. Reserved {format_cost(item.get('reserved_micros'))}.")
    if reports:
        lines.extend(report_sentence(record) for record in reports)
    else:
        lines.append("No score report recorded.")
    count = sum(len(turn["seqs"]) for turn in turns) + sum(
        (turn["inherited"] or {}).get("count", 0) for turn in turns
    )
    lines.append(
        f"Evidence holds {count} events across {len(turns)} turn{'s' if len(turns) != 1 else ''}. "
        f'Run "environment-harness timeline {item["id"]}" for the turn view.'
    )
    return "\n".join(lines)


def render_comparison(result) -> str:
    records = result.get("environments") or []
    lineages = {record.get("lineage") for record in records}
    lines = [f"Compared {len(records)} environments in {len(lineages)} lineage{'s' if len(lineages) != 1 else ''}."]
    for record in records:
        report = (record.get("latest_report") or {}).get("report") or {}
        parts = [
            title(record),
            short_id(record["environment"]),
            record.get("status", ""),
            f"revision {record.get('revision')}",
            f"cost {format_cost(record.get('cost_micros'))}",
            scalars(report.get("metrics") or {}) or "no score report",
        ]
        if record.get("interventions"):
            parts.append(f"interventions {scalars(record['interventions'])}")
        lines.append("  " + "  ".join(parts))
    metrics = result.get("metrics") or {}
    if metrics:
        lines.append("Metrics")
        for key, summary in metrics.items():
            error = summary.get("standard_error")
            lines.append(
                f"  {key}  mean of lineage means {compact(summary.get('mean_of_lineage_means'))}"
                f"  independent lineages {summary.get('independent_lineages')}"
                f"  standard error {compact(error) if error is not None else 'not available'}"
            )
    for key in ("uncertainty", "design"):
        if result.get(key):
            lines.append(result[key])
    return "\n".join(lines)
