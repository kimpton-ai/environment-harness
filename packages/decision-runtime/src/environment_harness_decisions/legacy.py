"""Read-only readers for pre-decision-runtime Minecraft run journals.

The reader never opens the application store through its normal constructor,
because that constructor repairs restart state.  Migration code can therefore
inspect a saved run and copy its evidence without changing the source.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LEGACY_READER_VERSION = "minecraft-legacy-reader@1"


def _read_table(db: sqlite3.Connection, table: str) -> tuple[dict[str, Any], ...]:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return ()
    return tuple(dict(row) for row in db.execute(f"SELECT * FROM {table}"))


@dataclass(frozen=True)
class LegacyRun:
    source: Path
    databases: tuple[Path, ...]
    goals: tuple[dict[str, Any], ...]
    messages: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    leases: tuple[dict[str, Any], ...]
    prepared_successors: tuple[dict[str, Any], ...]

    @property
    def unresolved_dispatches(self):
        return tuple(row for row in self.attempts if row.get("status") in {"unknown", "pending"})

    @property
    def unresolved_charges(self):
        return tuple(row for row in self.attempts if row.get("status") not in {"resolved", "settled", "rejected_before_dispatch"})

    @property
    def unresolved_prepared_successors(self):
        return tuple(row for row in self.prepared_successors if row.get("status") in {"prepared", "unknown", "dispatching"})

    def identity(self) -> dict[str, Any]:
        return {"reader": LEGACY_READER_VERSION, "source": str(self.source),
                "databases": [str(path) for path in self.databases],
                "goals": len(self.goals), "events": len(self.events)}


def _database_paths(root: Path) -> tuple[Path, ...]:
    if root.is_file():
        return (root,) if root.suffix == ".sqlite" else ()
    return tuple(sorted(path for path in root.glob("**/*.sqlite") if path.is_file()))


def read_legacy_run(path: str | Path) -> LegacyRun:
    root = Path(path).expanduser().resolve()
    databases = _database_paths(root)
    goals = messages = events = attempts = leases = successors = ()
    for database in databases:
        db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            goals += _read_table(db, "goals")
            messages += _read_table(db, "messages")
            events += _read_table(db, "events")
            attempts += _read_table(db, "attempts")
            leases += _read_table(db, "operation_leases")
            successors += _read_table(db, "motor_successors")
        finally:
            db.close()
    return LegacyRun(root, databases, goals, messages, events, attempts, leases, successors)


def commit_segment(source: LegacyRun, destination: str | Path, *, source_identity: dict[str, Any] | None = None,
                   destination_identity: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a linked SDK segment.  This only writes the destination."""
    target = Path(destination).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=False)
    copied = []
    for database in source.databases:
        output = target / database.name
        shutil.copy2(database, output)
        copied.append({"source": str(database), "destination": str(output),
                       "sha256": hashlib.sha256(output.read_bytes()).hexdigest()})
    manifest = {"schema": "decision-runtime-migration@1", "migration_id": uuid.uuid4().hex,
                "reader": LEGACY_READER_VERSION, "source_identity": source_identity or source.identity(),
                "destination_identity": destination_identity or {}, "databases": copied,
                "budgets_retained": True, "evidence_retained": True, "phase": "committed"}
    (target / "source-link.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
