"""Read-only readers for pre-decision-runtime run journals.

The reader never opens the application store through its normal constructor,
because that constructor repairs restart state.  Migration code can therefore
inspect a saved run and copy its evidence without changing the source.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEGACY_READER_VERSION = "legacy-run-reader@1"


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
        from .migration import readonly_database
        with readonly_database(database) as db:
            goals += _read_table(db, "goals")
            messages += _read_table(db, "messages")
            events += _read_table(db, "events")
            attempts += _read_table(db, "attempts")
            leases += _read_table(db, "operation_leases")
            successors += _read_table(db, "motor_successors")
    
    return LegacyRun(root, databases, goals, messages, events, attempts, leases, successors)
