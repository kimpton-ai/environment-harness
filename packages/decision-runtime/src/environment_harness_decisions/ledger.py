"""Private, crash durable child ledger beneath one SDK operation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from environment_harness.errors import Conflict


def encode(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS invocations (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, policy TEXT NOT NULL,
                    maximum INTEGER NOT NULL, expires_at TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, receipt TEXT,
                    timings TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, invocation TEXT NOT NULL, selection_id TEXT NOT NULL,
                    reservation INTEGER NOT NULL, status TEXT NOT NULL,
                    cost INTEGER, selection TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS effects (
                    id TEXT PRIMARY KEY, invocation TEXT NOT NULL, selection_id TEXT NOT NULL,
                    command TEXT NOT NULL, binding TEXT NOT NULL, before_observation TEXT NOT NULL,
                    status TEXT NOT NULL, native_receipt TEXT, verification TEXT);
                CREATE TABLE IF NOT EXISTS artifacts (
                    hash TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, invocation TEXT NOT NULL,
                    kind TEXT NOT NULL, artifact_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS control (
                    id INTEGER PRIMARY KEY CHECK(id=1), stop_epoch INTEGER NOT NULL);
                INSERT OR IGNORE INTO control VALUES(1,0);
                CREATE TABLE IF NOT EXISTS successors (
                    id TEXT PRIMARY KEY, invocation TEXT NOT NULL, intent TEXT NOT NULL,
                    status TEXT NOT NULL, admission TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_successor
                    ON successors((1)) WHERE status IN ('prepared','unknown');
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def ownership(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".owner")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, 0)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise Conflict("decision environment already owned") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Conflict("decision environment already owned") from exc
            yield
        finally:
            os.close(fd)

    def event(self, db, invocation, kind, payload):
        serialized = encode(payload)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?)", (digest, serialized))
        db.execute(
            "INSERT INTO events(invocation,kind,artifact_hash) VALUES (?,?,?)", (invocation, kind, digest)
        )
        return digest

    @property
    def stop_epoch(self):
        """Read the stop epoch without taking the writer lock.

        This is polled every 10 ms by the selection loop and every 25 ms by the
        authority watcher. ``db()`` opens with ``BEGIN IMMEDIATE``, so routing
        this read through it serialized every poll against the inference
        thread's ledger writes to read a single integer. WAL already gives a
        reader a consistent snapshot without blocking a writer.
        """

        db = sqlite3.connect(self.path, timeout=5)
        try:
            return db.execute("SELECT stop_epoch FROM control WHERE id=1").fetchone()[0]
        finally:
            db.close()

    def charges(self, db, invocation):
        rows = db.execute("SELECT * FROM attempts WHERE invocation=?", (invocation,)).fetchall()
        return (
            sum(row["cost"] or 0 for row in rows if row["status"] == "resolved"),
            sum(row["reservation"] for row in rows if row["status"] != "resolved"),
            all(row["status"] == "resolved" for row in rows),
        )
