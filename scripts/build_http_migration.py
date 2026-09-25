"""Generate and enforce the HTTP 0.2 → 0.3 migration manifest.

The frozen pre-change inventory at ``contracts/migrations/http-0.2-operations.json``
records every published 0.2 operation. This script classifies each one exactly
once, recomputes the candidate OpenAPI fingerprints and the internal
authorization-registry fingerprint, and generates the human migration and
authorization-change tables so documentation cannot drift from enforcement.

Run with ``--check`` in CI. Any unclassified or unknown operation fails closed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from environment_harness import EvidenceStore
from environment_harness.access import (
    ISSUABLE_POLICIES,
    POLICY_ACTIONS,
    registry_fingerprint,
)
from environment_harness.access import (
    fingerprint as canonical_fingerprint,
)
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runtime import _SessionRuntime
from environment_harness.server import create_app

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "contracts/migrations/http-0.2-operations.json"
MANIFEST = ROOT / "contracts/migrations/http-0.2-to-0.3.json"
TABLE = ROOT / "docs/HTTP-MIGRATION.md"

#: Every old operation classified exactly once. ``mapped`` names the destination
#: operationId in the candidate document; ``removed`` names no destination.
CLASSIFICATIONS: dict[str, dict[str, object]] = {
    "health_health_get": {
        "disposition": "retained",
        "destination": "health_health_get",
        "route": "unchanged",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "unchanged",
        "resource_constraints": "unchanged",
        "rationale": "Operational health is unauthenticated and unchanged.",
    },
    "environment_v1_environment_get": {
        "disposition": "removed",
        "destination": None,
        "route": "breaking",
        "request": "breaking",
        "response": "breaking",
        "pagination": "unchanged",
        "errors": "breaking",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "breaking",
        "rationale": (
            "There is no Environment singleton. EnvironmentSpec is frozen inside each Experiment "
            "and inherited by its Sessions; read it from Experiment.spec.environment or "
            "Session.spec.environment."
        ),
    },
    "create_v1_environments_post": {
        "disposition": "mapped",
        "destination": "create_experiment_v1_experiments_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "breaking",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "Every Session is derived from an Experiment, so the supported one-off path creates an "
            "implicit experiment-of-one. The response now returns 201 with a Location header."
        ),
    },
    "environments_v1_environments_get": {
        "disposition": "mapped",
        "destination": "sessions_v1_sessions_get",
        "route": "breaking",
        "request": "compatible",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "compatible",
        "rationale": (
            "The bare list is replaced by the typed management envelope with an opaque cursor, and "
            "items are portable Session resources."
        ),
    },
    "get_v1_environments__environment__get": {
        "disposition": "mapped",
        "destination": "get_session_v1_sessions__session_id__get",
        "route": "breaking",
        "request": "unchanged",
        "response": "breaking",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "compatible",
        "rationale": "The response is the portable Session resource with independent status dimensions.",
    },
    "observation_v1_environments__environment__observation_get": {
        "disposition": "mapped",
        "destination": ("observation_v1_sessions__session_id__participants__participant_id__observation_get"),
        "route": "breaking",
        "request": "breaking",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "breaking",
        "rationale": "Participant scope moves from a query parameter into genuine path ownership.",
    },
    "action_v1_environments__environment__actions_post": {
        "disposition": "mapped",
        "destination": "submit_action_v1_sessions__session_id__participants__participant_id__actions_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "breaking",
        "rationale": "Only a participant credential bound to this session and participant may submit.",
    },
    "credential_v1_environments__environment__credentials_post": {
        "disposition": "mapped",
        "destination": ("credential_v1_sessions__session_id__participants__participant_id__credentials_post"),
        "route": "breaking",
        "request": "breaking",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "breaking",
        "rationale": (
            "The participant is named by the route, never the body, and the operation declares the "
            "participant-credentials capability."
        ),
    },
    "command_v1_environments__environment__commands_post": {
        "disposition": "mapped",
        "destination": "command_v1_sessions__session_id__commands_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "breaking",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "compatible",
        "rationale": (
            "Queued lifecycle commands return 202 with a typed receipt whose result carries the "
            "previous body."
        ),
    },
    "events_v1_environments__environment__events_get": {
        "disposition": "mapped",
        "destination": "evidence_v1_sessions__session_id__evidence_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "compatible",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "compatible",
        "rationale": (
            "Renamed to `evidence` and given a typed EvidencePage response; the durable integer "
            "cursor and SSE resume are unchanged."
        ),
    },
    "agent_work_v1_environments__environment__agent_work_get": {
        "disposition": "mapped",
        "destination": "invocations_v1_sessions__session_id__invocations_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "breaking",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "compatible",
        "rationale": "Hyphenated `agent-work` becomes `invocations`; the body key becomes `items`.",
    },
    "prepare_v1_environments__environment__operations_post": {
        "disposition": "mapped",
        "destination": "prepare_v1_sessions__session_id__operations_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Route rename only; operation preparation remains a participant operation.",
    },
    "artifact_v1_environments__environment__artifacts_post": {
        "disposition": "mapped",
        "destination": "artifact_v1_sessions__session_id__artifacts_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Route rename only. Oversize bodies now return the payload_too_large code.",
    },
    "read_artifact_v1_environments__environment__artifacts__key__get": {
        "disposition": "mapped",
        "destination": "read_artifact_v1_sessions__session_id__artifacts__artifact_id__get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Route rename and a named `artifact_id` path parameter.",
    },
    "reports_v1_environments__environment__reports_get": {
        "disposition": "mapped",
        "destination": "scores_v1_sessions__session_id__scores_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "breaking",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "ScoreReport is returned through contextual `/scores` collections. The Python contract "
            "keeps its name; the body key becomes `items`."
        ),
    },
    "report_v1_environments__environment__reports_post": {
        "disposition": "mapped",
        "destination": "publish_score_v1_sessions__session_id__scores_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "compatible",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Publishing a report returns 201 with Location and an ETag on the stored hash.",
    },
    "environment_turn_series_v1_environments__environment__turn_series_get": {
        "disposition": "mapped",
        "destination": "session_turn_series_v1_sessions__session_id__turn_series_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Route rename only.",
    },
    "export_v1_environments__environment__export_get": {
        "disposition": "removed",
        "destination": None,
        "route": "breaking",
        "request": "breaking",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "breaking",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "breaking",
        "rationale": (
            "The action-row `environment-rollout.v1` export is corrected once before 0.3.0rc1. "
            "Freeze a TrajectorySnapshot and read `/v1/snapshots/{id}/records`, or freeze a "
            "TrajectoryDataset and read `/v1/datasets/{id}/records`, with `Accept: "
            "application/x-ndjson` for streaming. Stored evidence stays readable through the "
            "trajectory projection."
        ),
    },
    "environment_activity_v1_environments__environment__activity_get": {
        "disposition": "mapped",
        "destination": "session_activity_v1_sessions__session_id__activity_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Route rename only; the durable activity cursor is preserved.",
    },
    "global_activity_v1_activity_events_get": {
        "disposition": "mapped",
        "destination": "global_activity_v1_activity_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "`/v1/activity/events` becomes the collection itself at `/v1/activity`.",
    },
    "activity_snapshot_v1_activity_snapshot_get": {
        "disposition": "mapped",
        "destination": "activity_hierarchy_v1_activity_hierarchy_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "compatible",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "`snapshot` is the name of the immutable TrajectorySnapshot resource, so the activity "
            "projection is renamed `hierarchy`. `ActivitySnapshot` becomes `ActivityHierarchy`."
        ),
    },
    "experiment_activity_v1_experiments__experiment__events_get": {
        "disposition": "mapped",
        "destination": "experiment_activity_v1_experiments__experiment_id__activity_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Experiment activity is scoped consistently at `/activity`.",
    },
    "comparison_v1_compare_post": {
        "disposition": "mapped",
        "destination": "comparison_v1_comparisons_post",
        "route": "breaking",
        "request": "breaking",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "The verb path becomes the plural resource, and the request key `environments` becomes "
            "`sessions`. No comparison authority is persisted."
        ),
    },
    "trajectory_index_v1_trajectories_get": {
        "disposition": "mapped",
        "destination": "trajectory_index_v1_trajectories_get",
        "route": "unchanged",
        "request": "unchanged",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "The bare list becomes the typed management envelope.",
    },
    "trajectory_resource_v1_trajectories__trajectory__get": {
        "disposition": "mapped",
        "destination": "trajectory_resource_v1_trajectories__trajectory_id__get",
        "route": "compatible",
        "request": "unchanged",
        "response": "breaking",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "`status.records` is removed. Read records through "
            "`/v1/trajectories/{id}/records`; status now carries segments, a record count, the "
            "sequence boundary, and typed collection health."
        ),
    },
    "trajectory_records_v1_trajectories__trajectory__records_get": {
        "disposition": "mapped",
        "destination": "trajectory_records_v1_trajectories__trajectory_id__records_get",
        "route": "compatible",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Path parameter rename only; the durable integer sequence cursor is preserved.",
    },
    "freeze_trajectory_snapshot_v1_trajectories__trajectory__snapshots_post": {
        "disposition": "mapped",
        "destination": "freeze_trajectory_snapshot_v1_trajectories__trajectory_id__snapshots_post",
        "route": "compatible",
        "request": "unchanged",
        "response": "compatible",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Creation now returns 201 with a Location header for the frozen snapshot.",
    },
    "trajectory_snapshots_v1_trajectory_snapshots_get": {
        "disposition": "mapped",
        "destination": "trajectory_snapshots_v1_trajectories__trajectory_id__snapshots_get",
        "route": "breaking",
        "request": "breaking",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "Listing a trajectory's snapshots is nested under its owner; `/v1/snapshots` remains a "
            "top-level index because a snapshot is independently addressable."
        ),
    },
    "trajectory_snapshot_v1_trajectory_snapshots__snapshot__get": {
        "disposition": "mapped",
        "destination": "trajectory_snapshot_v1_snapshots__snapshot_id__get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "One-word plural collection name plus an ETag on the snapshot digest.",
    },
    "export_trajectory_snapshot_v1_trajectory_snapshots__snapshot__export_get": {
        "disposition": "mapped",
        "destination": "snapshot_records_v1_snapshots__snapshot_id__records_get",
        "route": "breaking",
        "request": "compatible",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "Content negotiation replaces the `/export` verb path: the typed JSON cursor page is "
            "the generated-client response and `Accept: application/x-ndjson` streams NDJSON."
        ),
    },
    "freeze_trajectory_dataset_v1_trajectory_datasets_post": {
        "disposition": "mapped",
        "destination": "freeze_trajectory_dataset_v1_datasets_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "compatible",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "One-word plural collection name; creation returns 201 with Location.",
    },
    "trajectory_datasets_v1_trajectory_datasets_get": {
        "disposition": "mapped",
        "destination": "trajectory_datasets_v1_datasets_get",
        "route": "breaking",
        "request": "compatible",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "One-word plural collection name plus the typed management envelope.",
    },
    "trajectory_dataset_v1_trajectory_datasets__dataset__get": {
        "disposition": "mapped",
        "destination": "trajectory_dataset_v1_datasets__dataset_id__get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "One-word plural collection name plus an ETag on the dataset digest.",
    },
    "export_trajectory_dataset_v1_trajectory_datasets__dataset__export_get": {
        "disposition": "mapped",
        "destination": "dataset_records_v1_datasets__dataset_id__records_get",
        "route": "breaking",
        "request": "compatible",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Content negotiation replaces the `/export` verb path.",
    },
    "training_runs_v1_training_runs_get": {
        "disposition": "mapped",
        "destination": "training_runs_v1_training_runs_get",
        "route": "unchanged",
        "request": "compatible",
        "response": "breaking",
        "pagination": "breaking",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "The bare list becomes the typed management envelope.",
    },
    "training_run_v1_training_runs__training_run__get": {
        "disposition": "mapped",
        "destination": "training_run_v1_training_runs__training_run_id__get",
        "route": "compatible",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Path parameter rename only.",
    },
    "register_trajectory_source_v1_trajectory_sources_post": {
        "disposition": "mapped",
        "destination": "register_source_v1_sources_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "compatible",
        "pagination": "unchanged",
        "errors": "breaking",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "One-word plural collection name, 201 with Location, and a declared "
            "historical-ingestion capability that returns 501 when disabled instead of hiding the "
            "route."
        ),
    },
    "ingest_trajectory_records_v1_trajectory_sources__source__records_post": {
        "disposition": "mapped",
        "destination": "ingest_source_records_v1_sources__source_id__records_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "breaking",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "Route rename plus the declared historical-ingestion capability.",
    },
    "update_trajectory_source_status_v1_trajectory_sources__source__status_put": {
        "disposition": "mapped",
        "destination": "report_source_status_v1_sources__source_id__status_reports_post",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "breaking",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": (
            "Declaring collection health is an appended status report, not an idempotent PUT, so "
            "the operation becomes `POST /v1/sources/{id}/status-reports` and returns 201."
        ),
    },
    "trajectory_source_status_v1_trajectory_sources__source__status_get": {
        "disposition": "mapped",
        "destination": "source_status_v1_sources__source_id__status_get",
        "route": "breaking",
        "request": "unchanged",
        "response": "unchanged",
        "pagination": "unchanged",
        "errors": "compatible",
        "authentication": "unchanged",
        "access_policy": "breaking",
        "resource_constraints": "unchanged",
        "rationale": "One-word plural collection name.",
    },
}

#: Operations outside the public resource hierarchy. They are deliberately not
#: part of this inventory and keep their own contracts and tests.
OUT_OF_HIERARCHY = {
    "/local/connect": "loopback viewer handshake on the operational routes",
    "/viewer/config": "operational viewer configuration",
    "/v1/worker/call": "separate create_worker_app process on environment-worker.v1",
}


def candidate_document() -> dict:
    with tempfile.TemporaryDirectory(prefix="environment-harness-migration-") as directory:
        runtime = _SessionRuntime(EvidenceStore(directory), SyntheticEnvironment())
        return create_app(runtime, trajectory_ingestion=True).openapi()


def operations(document: dict) -> dict[str, dict]:
    found = {}
    for path, item in sorted(document["paths"].items()):
        for method, operation in sorted(item.items()):
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            found[operation["operationId"]] = {
                "method": method.upper(),
                "path": path,
                "summary": operation.get("summary"),
                "capability": operation.get("x-capability"),
                "security": operation.get("security"),
                "responses": sorted(operation.get("responses", {})),
            }
    return found


def fingerprint(entries: dict[str, dict]) -> str:
    return canonical_fingerprint(
        {name: {"method": entry["method"], "path": entry["path"]} for name, entry in sorted(entries.items())}
    )


def build() -> tuple[dict, str]:
    inventory = json.loads(INVENTORY.read_text())
    old = {entry["operationId"]: entry for entry in inventory["operations"]}
    document = candidate_document()
    current = operations(document)

    unclassified = sorted(set(old) - set(CLASSIFICATIONS))
    if unclassified:
        raise SystemExit("unclassified 0.2 operations: " + ", ".join(unclassified))
    unknown = sorted(set(CLASSIFICATIONS) - set(old))
    if unknown:
        raise SystemExit("classified operations that are not in the frozen inventory: " + ", ".join(unknown))

    seen: dict[str, str] = {}
    entries = []
    for name in sorted(old):
        classification = dict(CLASSIFICATIONS[name])
        if name in seen:
            raise SystemExit("duplicated old operation: " + name)
        seen[name] = name
        destination = classification["destination"]
        if classification["disposition"] == "mapped" and destination not in current:
            raise SystemExit(f"mapped destination is missing from the candidate document: {name}")
        if classification["disposition"] == "removed" and destination is not None:
            raise SystemExit(f"a removed operation cannot declare a destination: {name}")
        if not classification.get("rationale"):
            raise SystemExit("every classification requires a rationale: " + name)
        entries.append(
            {
                "operationId": name,
                "method": old[name]["method"],
                "path": old[name]["path"],
                "previousAuthorization": old[name]["authorization"],
                **classification,
                "destinationMethod": current[destination]["method"] if destination else None,
                "destinationPath": current[destination]["path"] if destination else None,
                "destinationCapability": current[destination]["capability"] if destination else None,
            }
        )

    # Every new authenticated operation must register an internal access policy.
    unregistered = [
        name
        for name, entry in current.items()
        if entry["security"] and not any(entry["path"].startswith(prefix) for prefix in ("/v1",))
    ]
    if unregistered:
        raise SystemExit("authenticated operations outside /v1: " + ", ".join(sorted(unregistered)))
    unauthenticated = sorted(
        name for name, entry in current.items() if entry["path"].startswith("/v1") and not entry["security"]
    )
    if unauthenticated:
        raise SystemExit("unauthenticated /v1 operations: " + ", ".join(unauthenticated))

    manifest = {
        "schema": "environment-harness.http-migration.v1",
        "from": inventory["package"],
        "to": "0.3.0rc1",
        "contract": "environmentharness.dev/v1alpha1",
        "previousFingerprint": fingerprint(
            {entry["operationId"]: entry for entry in inventory["operations"]}
        ),
        "candidateFingerprint": fingerprint(current),
        "authorizationRegistryFingerprint": registry_fingerprint(),
        "authorizationModel": {
            "previous": inventory["authorization_model"],
            "current": {
                "kind": "server-owned credential policies",
                "issuable": list(ISSUABLE_POLICIES),
                "policies": {name: sorted(actions) for name, actions in sorted(POLICY_ACTIONS.items())},
            },
        },
        "outOfHierarchy": OUT_OF_HIERARCHY,
        "operations": entries,
        "addedOperations": sorted(
            name
            for name in current
            if name not in {entry.get("destination") for entry in CLASSIFICATIONS.values()}
        ),
    }
    return manifest, table(manifest, current)


def table(manifest: dict, current: dict[str, dict]) -> str:
    dimensions = (
        "route",
        "request",
        "response",
        "pagination",
        "errors",
        "authentication",
        "access_policy",
        "resource_constraints",
    )
    lines = [
        "# HTTP 0.2 → 0.3 migration",
        "",
        "<!-- Generated by scripts/build_http_migration.py. Do not edit by hand. -->",
        "",
        "Every operation published by "
        f"`environment-harness=={manifest['from']}` is classified exactly once below. The",
        "machine-readable source is [`contracts/migrations/http-0.2-to-0.3.json`]"
        "(../contracts/migrations/http-0.2-to-0.3.json),",
        "and CI regenerates this page so documentation cannot drift from enforcement.",
        "",
        "Consumers such as EvalRouter must pin "
        f"`environment-harness=={manifest['from']}` until their adapter is migrated.",
        "",
        "## Route and contract changes",
        "",
        "| Old operation | New operation | Disposition | Breaking dimensions | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in manifest["operations"]:
        breaking = [name for name in dimensions if entry[name] == "breaking"]
        destination = (
            f"`{entry['destinationMethod']} {entry['destinationPath']}`"
            if entry["destinationPath"]
            else "— removed"
        )
        lines.append(
            f"| `{entry['method']} {entry['path']}` | {destination} | {entry['disposition']} | "
            + (", ".join(breaking) if breaking else "none")
            + f" | {entry['rationale']} |"
        )
    lines += [
        "",
        "## Authorization changes",
        "",
        "The public four-role model is removed. A caller sends only an opaque bearer credential and",
        "the server resolves it to one of these fixed policies. Requests never assert a policy,",
        "role, or permission. See [Authentication](AUTHENTICATION.md).",
        "",
        "| Credential policy | Issuable | Registered actions |",
        "| --- | --- | --- |",
    ]
    for name, actions in sorted(manifest["authorizationModel"]["current"]["policies"].items()):
        issuable = "yes" if name in manifest["authorizationModel"]["current"]["issuable"] else "no"
        lines.append(f"| `{name}` | {issuable} | {len(actions)} |")
    lines += [
        "",
        f"Authorization-registry fingerprint: `{manifest['authorizationRegistryFingerprint']}`.",
        "",
        "Every old operation's authorization behavior changed, because the roles it accepted no",
        "longer exist. All pre-`0.3.0rc1` credentials are deleted by migration",
        "`005_credential_policies` and must be reissued; old bearer tokens return `401`.",
        "",
        "## Operations outside the public hierarchy",
        "",
        "| Route | Why it is separate |",
        "| --- | --- |",
    ]
    for path, reason in sorted(manifest["outOfHierarchy"].items()):
        lines.append(f"| `{path}` | {reason} |")
    lines += [
        "",
        "## Operations added in 0.3.0rc1",
        "",
        "These have no 0.2 predecessor.",
        "",
        "| Operation |",
        "| --- |",
    ]
    for name in manifest["addedOperations"]:
        entry = current[name]
        lines.append(f"| `{entry['method']} {entry['path']}` |")
    return "\n".join(lines) + "\n"


def main() -> None:
    manifest, generated = build()
    body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        for path, expected in ((MANIFEST, body), (TABLE, generated)):
            if not path.exists() or path.read_text() != expected:
                raise SystemExit("HTTP migration drift: run `uv run python scripts/build_http_migration.py`")
        print(f"HTTP migration manifest is current: {len(manifest['operations'])} classified operations")
        return
    MANIFEST.write_text(body)
    TABLE.write_text(generated)
    print(f"Wrote {MANIFEST.relative_to(ROOT)} and {TABLE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
