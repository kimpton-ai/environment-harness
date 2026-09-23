"""Transactional evidence and authorization. SQLite is the local authority."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .contracts import Principal, ScoreReport
from .errors import Conflict, Forbidden


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def uid() -> str:
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS environments (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, manifest TEXT NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, scheduler TEXT NOT NULL,
 rng TEXT NOT NULL, participants TEXT NOT NULL, cursors TEXT NOT NULL,
 status TEXT NOT NULL, lineage TEXT NOT NULL, parent TEXT, checkpoint TEXT,
 lease_owner TEXT, lease_epoch INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
 spent INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
 environment TEXT NOT NULL, seq INTEGER NOT NULL, revision INTEGER NOT NULL,
 kind TEXT NOT NULL, body TEXT NOT NULL, audience TEXT NOT NULL, event_time REAL,
 ingested REAL NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL,
 PRIMARY KEY(environment,seq));
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, environment TEXT NOT NULL, participant TEXT NOT NULL,
 generation INTEGER NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
 UNIQUE(environment,participant,generation,revision));
CREATE TABLE IF NOT EXISTS actions (
 environment TEXT NOT NULL, id TEXT NOT NULL, participant TEXT NOT NULL,
 revision INTEGER NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
 receipt TEXT NOT NULL, PRIMARY KEY(environment,id));
CREATE UNIQUE INDEX IF NOT EXISTS one_action_per_phase ON actions(environment,participant,revision)
 WHERE status IN ('accepted','committed');
CREATE TABLE IF NOT EXISTS checkpoints (
 id TEXT PRIMARY KEY, environment TEXT NOT NULL, revision INTEGER NOT NULL,
 body TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
 environment TEXT NOT NULL, id TEXT NOT NULL, participant TEXT NOT NULL,
 generation INTEGER NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
 receipt TEXT, reservation INTEGER NOT NULL, PRIMARY KEY(environment,id));
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, environment TEXT NOT NULL, audience TEXT NOT NULL,
 sha256 TEXT NOT NULL, size INTEGER NOT NULL, media_type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
 environment TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL,
 PRIMARY KEY(environment,revision));
CREATE TABLE IF NOT EXISTS trajectory_snapshots (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, environment TEXT NOT NULL,
 body TEXT NOT NULL, digest TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS trajectory_snapshot_records (
 snapshot TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(snapshot,sequence));
CREATE TABLE IF NOT EXISTS trajectory_sources (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, namespace TEXT NOT NULL, run_id TEXT NOT NULL,
 registration TEXT NOT NULL, registration_hash TEXT NOT NULL,
 collection_state TEXT NOT NULL, execution_state TEXT NOT NULL,
 termination TEXT NOT NULL, verified_outcome TEXT NOT NULL,
 acknowledged_position TEXT, acknowledged_hash TEXT,
 backlog INTEGER, gaps TEXT NOT NULL, capture_failures TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(tenant,namespace,run_id));
CREATE TABLE IF NOT EXISTS trajectory_source_records (
 source TEXT NOT NULL, ordinal INTEGER NOT NULL, record_id TEXT NOT NULL, position TEXT NOT NULL,
 source_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(source,ordinal), UNIQUE(source,record_id), UNIQUE(source,position));
CREATE TABLE IF NOT EXISTS trajectory_datasets (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS training_runs (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, dataset TEXT NOT NULL, body TEXT NOT NULL,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_aliases (
 environment TEXT NOT NULL, alias TEXT NOT NULL, artifact TEXT NOT NULL, PRIMARY KEY(environment,alias));
CREATE TABLE IF NOT EXISTS credentials (
 hash TEXT PRIMARY KEY, principal TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS splits (
 tenant TEXT NOT NULL, environment TEXT NOT NULL, scenario TEXT NOT NULL,
 time_boundary TEXT NOT NULL, split TEXT NOT NULL,
 PRIMARY KEY(tenant,environment,scenario,time_boundary));
CREATE TABLE IF NOT EXISTS transitions (
 environment TEXT NOT NULL, revision INTEGER NOT NULL, input_hash TEXT NOT NULL,
 request TEXT NOT NULL, lease_epoch INTEGER NOT NULL, status TEXT NOT NULL,
 result TEXT, rng TEXT, PRIMARY KEY(environment,revision,input_hash));
CREATE TABLE IF NOT EXISTS agent_work (
 environment TEXT NOT NULL, revision INTEGER NOT NULL, participant TEXT NOT NULL,
 generation INTEGER NOT NULL, id TEXT NOT NULL, observation TEXT NOT NULL,
 status TEXT NOT NULL, response TEXT, agent_state TEXT,
 PRIMARY KEY(environment,revision,participant,generation));
CREATE TABLE IF NOT EXISTS experiments (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
 seed INTEGER NOT NULL, trials INTEGER NOT NULL, total INTEGER NOT NULL,
 completed INTEGER NOT NULL DEFAULT 0, running INTEGER NOT NULL DEFAULT 0,
 queued INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
 config TEXT NOT NULL, error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scenario_snapshots (
 experiment TEXT NOT NULL, scenario TEXT NOT NULL, position INTEGER NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(experiment,scenario));
CREATE TABLE IF NOT EXISTS session_runs (
 environment TEXT PRIMARY KEY, tenant TEXT NOT NULL, experiment TEXT, scenario TEXT NOT NULL,
 trial INTEGER NOT NULL, seed INTEGER NOT NULL, status TEXT NOT NULL, error TEXT,
 turns INTEGER NOT NULL DEFAULT 0, target_turns INTEGER NOT NULL,
 latest_activity TEXT, scenario_body TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS session_runs_experiment ON session_runs(experiment,status,scenario,trial);
CREATE TABLE IF NOT EXISTS event_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, topic TEXT NOT NULL,
 experiment TEXT, environment TEXT, kind TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS event_outbox_scope ON event_outbox(tenant,id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS reports_no_update BEFORE UPDATE ON reports BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS reports_no_delete BEFORE DELETE ON reports BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS checkpoints_no_update BEFORE UPDATE ON checkpoints BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS checkpoints_no_delete BEFORE DELETE ON checkpoints BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_snapshots_no_update BEFORE UPDATE ON trajectory_snapshots
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_snapshots_no_delete BEFORE DELETE ON trajectory_snapshots
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_snapshot_records_no_update
 BEFORE UPDATE ON trajectory_snapshot_records BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_snapshot_records_no_delete
 BEFORE DELETE ON trajectory_snapshot_records BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_source_records_no_update
 BEFORE UPDATE ON trajectory_source_records BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_source_records_no_delete
 BEFORE DELETE ON trajectory_source_records BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_datasets_no_update BEFORE UPDATE ON trajectory_datasets
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS trajectory_datasets_no_delete BEFORE DELETE ON trajectory_datasets
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS training_runs_no_update BEFORE UPDATE ON training_runs
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS training_runs_no_delete BEFORE DELETE ON training_runs
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""


class EvidenceStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.db_path = self.root / "evidence.sqlite"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        os.chmod(self.db_path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA cache_size=-4096")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _environment_row(self, db, environment):
        return db.execute("SELECT * FROM environments WHERE id=?", (environment,)).fetchone()

    def environment(self, db, environment: str, who: Principal, roles=None):
        row = self._environment_row(db, environment)
        if (
            not row
            or row["tenant"] != who.tenant
            or (who.environment is not None and who.environment != environment)
        ):
            raise Forbidden("environment unavailable")
        if roles and who.role not in roles:
            raise Forbidden("role cannot perform this operation")
        if who.role == "agent":
            p = json.loads(row["participants"]).get(who.participant)
            if (
                not p
                or not p["active"]
                or p["generation"] != who.generation
                or p["controller"] != who.subject
            ):
                raise Forbidden("participant authority expired")
        return row

    def append(self, db, environment, revision, kind, payload, audience=(), event_time=None):
        # REAL columns round-trip integers as floats. Hash the stored representation.
        event_time = float(event_time) if event_time is not None else None
        raw = encode(payload)
        manifest = json.loads(
            db.execute("SELECT manifest FROM environments WHERE id=?", (environment,)).fetchone()[0]
        )
        if len(raw.encode()) > manifest["policy"]["max_event_bytes"]:
            raise Conflict("event size limit exceeded; use an artifact")
        last = db.execute(
            "SELECT seq,hash FROM events WHERE environment=? ORDER BY seq DESC LIMIT 1", (environment,)
        ).fetchone()
        seq, previous = (last["seq"] + 1, last["hash"]) if last else (1, "0" * 64)
        event = dict(
            environment=environment,
            seq=seq,
            revision=revision,
            kind=kind,
            payload=payload,
            audience=list(audience),
            event_time=event_time,
            ingested=time.time(),
            previous=previous,
        )
        event["hash"] = digest(event)
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                environment,
                seq,
                revision,
                kind,
                raw,
                encode(list(audience)),
                event_time,
                event["ingested"],
                previous,
                event["hash"],
            ),
        )
        relation = db.execute(
            "SELECT tenant,experiment FROM session_runs WHERE environment=?", (environment,)
        ).fetchone()
        if relation:
            db.execute(
                "INSERT INTO event_outbox (tenant,topic,experiment,environment,kind,body,created) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    relation["tenant"],
                    "environment_session",
                    relation["experiment"],
                    environment,
                    kind,
                    raw,
                    time.time(),
                ),
            )
        return event

    def events(self, environment, who, after=0, limit=200):
        if after < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid event page")
        with self.transaction() as db:
            self.environment(db, environment, who)
            # Filter in SQL before limiting. Cursors reveal ordering, never hidden payloads.
            rows = self._event_page(db, environment, after, who, limit)
            events = [
                dict(
                    environment=r["environment"],
                    seq=r["seq"],
                    revision=r["revision"],
                    kind=r["kind"],
                    payload=json.loads(r["body"]),
                    audience=json.loads(r["audience"]),
                    event_time=r["event_time"],
                    ingested=r["ingested"],
                    previous=r["previous"],
                    hash=r["hash"],
                )
                for r in rows
            ]
            for event in events:
                timestamp = event["event_time"]
                if isinstance(timestamp, float) and timestamp.is_integer():
                    # Older writers hashed integral input before SQLite converted it to REAL.
                    original = {k: v for k, v in event.items() if k != "hash"}
                    original["event_time"] = int(timestamp)
                    if digest(original) == event["hash"]:
                        event["event_time"] = int(timestamp)
            return events

    def _event_page(self, db, environment, after, who, limit):
        return db.execute(
            """SELECT * FROM events WHERE environment=? AND seq>? AND
            (? IN ('researcher','scorer','worker') OR audience='["*"]' OR
             EXISTS(SELECT 1 FROM json_each(audience) WHERE value=?)) ORDER BY seq LIMIT ?""",
            (environment, after, who.role, who.participant, limit),
        ).fetchall()

    def replay(self, environment, who):
        cursor = 0
        while page := self.events(environment, who, cursor, 1000):
            yield from page
            cursor = page[-1]["seq"]

    def activity(self, who, after=0, limit=200, *, experiment=None, environment=None):
        from .activity import events

        return events(
            self,
            who,
            after,
            limit,
            experiment=experiment,
            environment=environment,
        )

    def activity_snapshot(self, who):
        if who.role not in ("researcher", "worker"):
            raise Forbidden("researcher activity authority required")

        def session_record(row):
            participants = list(json.loads(row["participants_json"])) if row["participants_json"] else []
            manifest = json.loads(row["manifest_json"]) if row["manifest_json"] else {}
            return {
                "kind": "session",
                "id": row["environment"],
                "scenario_id": row["scenario"],
                "trial": row["trial"] + 1,
                "status": row["status"],
                "current_turn": row["revision"] or 0,
                "target_turns": row["target_turns"],
                "participants": participants,
                "latest_activity": row["latest_activity"],
                "failure": row["error"],
                "environment": manifest.get("environment", {}),
                "frozen": manifest,
                "updated": row["updated"],
            }

        def aggregate_status(children):
            priority = {
                "succeeded": 0,
                "failed": 1,
                "stopped": 2,
                "interrupted": 3,
                "queued": 4,
                "running": 5,
            }
            return max(children, key=lambda child: priority[child["status"]])["status"]

        with self.transaction() as db:
            run_rows = db.execute(
                "SELECT r.*,e.revision,e.participants AS participants_json,e.manifest AS manifest_json "
                "FROM session_runs r LEFT JOIN environments e ON e.id=r.environment "
                "WHERE r.tenant=? ORDER BY r.created,r.scenario,r.trial",
                (who.tenant,),
            ).fetchall()
            records = {row["environment"]: session_record(row) for row in run_rows}
            experiments = []
            for experiment in db.execute(
                "SELECT * FROM experiments WHERE tenant=? ORDER BY created DESC", (who.tenant,)
            ):
                children = [
                    records[row["environment"]] for row in run_rows if row["experiment"] == experiment["id"]
                ]
                metric_values: dict[str, list[float]] = {}
                for child in children:
                    latest = db.execute(
                        "SELECT body FROM reports WHERE environment=? ORDER BY revision DESC LIMIT 1",
                        (child["id"],),
                    ).fetchone()
                    if latest:
                        for key, value in json.loads(latest["body"]).get("metrics", {}).items():
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                metric_values.setdefault(key, []).append(float(value))
                scores = {key: sum(values) / len(values) for key, values in sorted(metric_values.items())}
                latest = max(children, key=lambda child: child["updated"], default=None)
                scenarios = []
                for snapshot in db.execute(
                    "SELECT * FROM scenario_snapshots WHERE experiment=? ORDER BY position",
                    (experiment["id"],),
                ):
                    body = json.loads(snapshot["body"])
                    scenario_sessions = [
                        child for child in children if child["scenario_id"] == snapshot["scenario"]
                    ]
                    scenario_latest = max(scenario_sessions, key=lambda child: child["updated"], default=None)
                    scenario_counts = Counter(child["status"] for child in scenario_sessions)
                    scenario_status = aggregate_status(scenario_sessions)
                    scenarios.append(
                        {
                            "kind": "scenario",
                            "id": snapshot["scenario"],
                            "input": body["input"],
                            "reference": body.get("reference"),
                            "metadata": body.get("metadata", {}),
                            "status": scenario_status,
                            "completed": sum(
                                scenario_counts[status]
                                for status in ("succeeded", "failed", "stopped", "interrupted")
                            ),
                            "total": len(scenario_sessions),
                            "running": scenario_counts["running"],
                            "queued": scenario_counts["queued"],
                            "failed": scenario_counts["failed"],
                            "latest_activity": (
                                scenario_latest["latest_activity"] if scenario_latest else None
                            ),
                            "sessions": scenario_sessions,
                            "updated": scenario_latest["updated"] if scenario_latest else 0,
                        }
                    )
                experiments.append(
                    {
                        "kind": "experiment",
                        "id": experiment["id"],
                        "name": experiment["name"],
                        "status": experiment["status"],
                        "progress": {
                            "completed": experiment["completed"],
                            "total": experiment["total"],
                        },
                        "running": experiment["running"],
                        "queued": experiment["queued"],
                        "failed": experiment["failed"],
                        "latest_activity": latest["latest_activity"] if latest else None,
                        "score_summary": scores,
                        "frozen": json.loads(experiment["config"]),
                        "scenarios": scenarios,
                        "sessions": children,
                        "updated": experiment["updated"],
                    }
                )
            standalone = [records[row["environment"]] for row in run_rows if row["experiment"] is None]
            # Older advanced-runtime sessions predate orchestration records and remain top-level.
            for row in db.execute(
                "SELECT * FROM environments e WHERE tenant=? AND NOT EXISTS "
                "(SELECT 1 FROM session_runs r WHERE r.environment=e.id) ORDER BY id DESC",
                (who.tenant,),
            ):
                manifest = json.loads(row["manifest"])
                standalone.append(
                    {
                        "kind": "session",
                        "id": row["id"],
                        "scenario_id": manifest.get("scenario", "synthetic"),
                        "trial": 1,
                        "status": row["status"],
                        "current_turn": row["revision"],
                        # Advanced-runtime sessions have a safety cap, not a scheduled turn target.
                        "target_turns": None,
                        "participants": list(json.loads(row["participants"])),
                        "latest_activity": None,
                        "failure": None,
                        "environment": manifest.get("environment", {}),
                        "frozen": manifest,
                        "updated": 0,
                    }
                )
            summary = {
                "running": sum(row["status"] == "running" for row in records.values()),
                "queued": sum(row["status"] == "queued" for row in records.values()),
                "failed": sum(row["status"] == "failed" for row in records.values()),
            }
            cursor = db.execute(
                "SELECT coalesce(max(id),0) FROM event_outbox WHERE tenant=?", (who.tenant,)
            ).fetchone()[0]
            return {
                "summary": summary,
                "experiments": experiments,
                "standalone": standalone,
                "cursor": cursor,
            }

    def verify(self, environment, who):
        if who.role not in ("researcher", "scorer"):
            raise Forbidden("full evidence authority required")
        previous = "0" * 64
        count = 0
        for event in self.replay(environment, who):
            claimed = event.pop("hash")
            if event["previous"] != previous or digest(event) != claimed:
                raise Conflict("evidence integrity failure")
            previous = claimed
            count += 1
        return {"events": count, "head": previous}

    def issue(self, principal: Principal, ttl=3600):
        # Administrative embedding API only. Never exposed as an unauthenticated route.
        if not 1 <= ttl <= 86400 * 365:
            raise ValueError("invalid token lifetime")
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO credentials VALUES (?,?,?,0)",
                (hashlib.sha256(token.encode()).hexdigest(), principal.model_dump_json(), time.time() + ttl),
            )
        return token

    def authenticate(self, token):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM credentials WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
            ).fetchone()
            if not row or row["revoked"] or row["expires"] <= time.time():
                raise Forbidden("credential expired or invalid")
            return Principal.model_validate_json(row["principal"])

    def artifact(self, environment, who, data: bytes, audience=(), media_type="application/octet-stream"):
        with self.transaction() as db:
            row = self.environment(db, environment, who)
            if len(data) > json.loads(row["manifest"])["policy"]["max_artifact_bytes"]:
                raise Conflict("artifact size limit exceeded")
            participants = json.loads(row["participants"])
            if who.role == "agent":
                audience = (who.participant,)
            elif any(p not in participants and p != "*" for p in audience):
                raise ValueError("unknown artifact audience")
            key = uid()
            self._check_artifact_budget(db, environment, len(data))
            self._write_artifact(environment, key, data)
            sha = hashlib.sha256(data).hexdigest()
            db.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?)",
                (key, environment, encode(audience), sha, len(data), media_type),
            )
            self.append(
                db,
                environment,
                row["revision"],
                "artifact",
                {"id": key, "sha256": sha, "size": len(data)},
                audience,
            )
            return {"id": key, "sha256": sha, "size": len(data), "media_type": media_type}

    def _check_artifact_budget(self, db, environment, size):
        """Optional hosted aggregate storage admission under the environment lock."""
        return None

    def _write_artifact(self, environment, key, data):
        folder = self.root / "artifacts" / environment
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / key
        with path.open("xb") as f:
            os.chmod(path, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _read_artifact(self, environment, key):
        return (self.root / "artifacts" / environment / key).read_bytes()

    def read_artifact(self, environment, who, key):
        with self.transaction() as db:
            self.environment(db, environment, who)
            row = db.execute(
                "SELECT * FROM artifacts WHERE environment=? AND id=?", (environment, key)
            ).fetchone()
            if not row:
                row = db.execute(
                    "SELECT a.* FROM artifacts a JOIN artifact_aliases x ON x.artifact=a.id "
                    "AND x.environment=a.environment WHERE x.environment=? AND x.alias=?",
                    (environment, key),
                ).fetchone()
            if not row:
                raise Forbidden("artifact unavailable")
            audience = json.loads(row["audience"])
            if who.role == "agent" and "*" not in audience and who.participant not in audience:
                raise Forbidden("artifact unavailable")
            data = self._read_artifact(environment, row["id"])
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise Conflict("artifact integrity failure")
            return data, row["media_type"]

    def report(self, environment, who, report: ScoreReport):
        with self.transaction() as db:
            row = self.environment(db, environment, who, ("researcher", "scorer"))
            manifest = json.loads(row["manifest"])
            if f"{report.scorer}@{report.version}" not in manifest["scoring_versions"]:
                raise Conflict("scorer version is not frozen in experiment")
            last = db.execute(
                "SELECT coalesce(max(seq),0) FROM events WHERE environment=?", (environment,)
            ).fetchone()[0]
            if report.evidence_cursor > last:
                raise Conflict("report references unavailable evidence")
            for finding in report.findings:
                observation = db.execute(
                    "SELECT * FROM observations WHERE environment=? AND id=?",
                    (environment, finding.observation_id),
                ).fetchone()
                action = db.execute(
                    "SELECT * FROM actions WHERE environment=? AND id=?", (environment, finding.action_id)
                ).fetchone()
                if finding.action_id is None:
                    event = db.execute(
                        "SELECT * FROM events WHERE environment=? AND seq=? AND seq<=?",
                        (environment, finding.opportunity_event, report.evidence_cursor),
                    ).fetchone()
                    outcome = db.execute(
                        "SELECT * FROM events WHERE environment=? AND seq=? AND seq<=?",
                        (environment, finding.outcome_event, report.evidence_cursor),
                    ).fetchone()
                    if (
                        not observation
                        or observation["participant"] != finding.participant
                        or not event
                        or not outcome
                        or json.loads(outcome["body"]).get(
                            "actor", json.loads(outcome["body"]).get("participant")
                        )
                        != finding.participant
                        or json.loads(event["body"]).get(
                            "actor", json.loads(event["body"]).get("participant")
                        )
                        != finding.participant
                        or event["revision"] < observation["revision"]
                        or outcome["seq"] < event["seq"]
                    ):
                        raise Conflict("omission finding requires observation and opportunity evidence")
                    for seq in finding.consequence_events:
                        if not db.execute(
                            "SELECT 1 FROM events WHERE environment=? AND seq=? AND seq<=?",
                            (environment, seq, report.evidence_cursor),
                        ).fetchone():
                            raise Conflict("missing omission consequence evidence")
                    continue
                if (
                    not observation
                    or not action
                    or observation["participant"] != finding.participant
                    or action["participant"] != finding.participant
                ):
                    raise Conflict("finding evidence does not belong to participant")
                if json.loads(action["request"])["observation_id"] != finding.observation_id:
                    raise Conflict("finding observation does not support action")
                for seq in (finding.outcome_event, *finding.consequence_events):
                    event = db.execute(
                        "SELECT * FROM events WHERE environment=? AND seq=? AND seq<=?",
                        (environment, seq, report.evidence_cursor),
                    ).fetchone()
                    if not event:
                        raise Conflict("finding references missing evidence")
                    if seq == finding.outcome_event:
                        payload = json.loads(event["body"])
                        linked = (
                            payload.get("action_id")
                            if event["kind"] == "action.executed"
                            else payload.get("action", {}).get("operation_id")
                            if event["kind"] == "action.attempted"
                            else None
                        )
                        if linked != finding.action_id:
                            raise Conflict("finding outcome does not describe its action")
            revision = db.execute(
                "SELECT coalesce(max(revision),0)+1 FROM reports WHERE environment=?", (environment,)
            ).fetchone()[0]
            body = report.model_dump(mode="json")
            envelope = {"environment": environment, "revision": revision, "report": body}
            db.execute(
                "INSERT INTO reports VALUES (?,?,?,?)",
                (environment, revision, encode(body), digest(envelope)),
            )
            self.append(
                db, environment, row["revision"], "report", {"revision": revision, "hash": digest(envelope)}
            )
            return envelope | {"hash": digest(envelope)}

    def reports(self, environment, who):
        with self.transaction() as db:
            self.environment(db, environment, who, ("researcher", "scorer"))
            return [
                dict(
                    environment=environment,
                    revision=r["revision"],
                    report=json.loads(r["body"]),
                    hash=r["hash"],
                )
                for r in db.execute(
                    "SELECT * FROM reports WHERE environment=? ORDER BY revision", (environment,)
                )
            ]
