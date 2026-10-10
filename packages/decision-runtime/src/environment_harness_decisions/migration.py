"""Resumable migration mechanics. Consumers supply stopped-owner and reconciliation checks.

Nothing runs at import time. Reporting only reads. Applying never rewrites a
legacy artifact and must be called under the consumer's existing ownership lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .ledger import encode


class MigrationBlocked(RuntimeError):
    pass


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(encode(value) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def file_hash(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def readonly_database(source: Path):
    """Read a stopped snapshot without creating WAL or SHM files in the source.

    Concurrent source changes invalidate the report instead of constructing a
    mixed snapshot. Apply callers additionally hold the real controller lock.
    """
    source = Path(source).resolve()
    paths = [source]
    wal = source.with_name(source.name + "-wal")
    if wal.exists():
        paths.append(wal)
    signatures = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in paths}
    with tempfile.TemporaryDirectory(prefix="decision-migration-read-") as directory:
        copied = Path(directory) / source.name
        for path in paths:
            shutil.copyfile(path, Path(directory) / path.name)
        current = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in paths if path.exists()}
        if current != signatures or (wal.exists() and wal not in paths):
            raise MigrationBlocked("source database changed during read-only report")
        db = sqlite3.connect(copied)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def sqlite_backup(source: Path, destination: Path):
    """Consistent backup includes committed WAL data without checkpointing source."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    with readonly_database(source) as source_db:
        target_db = sqlite3.connect(destination)
        try:
            source_db.backup(target_db)
            if target_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationBlocked("backup integrity check failed")
        finally:
            target_db.close()
    with destination.open("rb") as file:
        os.fsync(file.fileno())


class MigrationTransaction:
    """One deterministic migration per destination version, with a final commit marker.

    ``check`` must prove the controller is stopped and there are no unresolved
    dispatches, charges or prepared successors. It runs before writing and again
    before commit. The caller holds the original controller's exclusive lock for
    the entire call. ``create_segment`` is idempotent and initializes an actual
    SDK segment, returning its identity and authoritative accounting linkage.
    """

    def __init__(
        self,
        run: Path,
        *,
        version: str,
        source_identity: dict,
        destination_identity: dict,
        source_files: list[Path],
    ):
        self.run = Path(run).resolve()
        self.source_identity = source_identity
        self.destination_identity = destination_identity
        token = hashlib.sha256(
            encode({"version": version, "destination": destination_identity}).encode()
        ).hexdigest()[:20]
        self.directory = self.run / ".decision-runtime" / token
        self.record_path = self.directory / "migration.json"
        self.files = tuple(sorted({Path(path).resolve() for path in source_files}))
        for path in self.files:
            if (
                not path.is_relative_to(self.run)
                or path.is_relative_to(self.run / ".decision-runtime")
                or path.is_symlink()
            ):
                raise ValueError("migration source must be an original run file")
        self.version = version

    def report(self):
        if not self.record_path.is_file():
            return {
                "version": self.version,
                "phase": "not_started",
                "source_identity": self.source_identity,
                "destination_identity": self.destination_identity,
                "read_only": True,
            }
        record = json.loads(self.record_path.read_text())
        self._identity(record)
        return {**record, "read_only": True}

    def _identity(self, record):
        if (
            record["version"] != self.version
            or record["source_identity"] != self.source_identity
            or record["destination_identity"] != self.destination_identity
        ):
            raise MigrationBlocked("migration identity changed")

    def _snapshot(self):
        try:
            return {str(path.relative_to(self.run)): file_hash(path) for path in self.files}
        except FileNotFoundError as error:
            raise MigrationBlocked("original evidence disappeared during migration") from error

    def apply(self, *, check: Callable[[], None], create_segment: Callable[[Path, dict], dict]):
        check()
        snapshot = self._snapshot()
        if self.record_path.exists():
            record = json.loads(self.record_path.read_text())
            self._identity(record)
            if record["source_hashes"] != snapshot:
                raise MigrationBlocked("original evidence changed since migration began")
            if record["phase"] == "committed":
                return record
        else:
            record = {
                "version": self.version,
                "phase": "backup_started",
                "source_identity": self.source_identity,
                "destination_identity": self.destination_identity,
                "source_hashes": snapshot,
                "backup_hashes": {},
                "original_retained": True,
            }
            atomic_json(self.record_path, record)
        backups = self.directory / "originals"
        for path in self.files:
            relative = str(path.relative_to(self.run))
            # Keep exact WAL bytes as evidence, never replay them over the
            # already consistent SQLite backup on restore.
            destination = backups / (
                relative + ".evidence" if relative.endswith(("-wal", "-shm")) else relative
            )
            if relative in record["backup_hashes"]:
                if not destination.is_file() or file_hash(destination) != record["backup_hashes"][relative]:
                    raise MigrationBlocked("migration backup changed")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("rb") as file:
                database = file.read(16) == b"SQLite format 3\x00"
            if database:
                sqlite_backup(path, destination)
            else:
                shutil.copyfile(path, destination)
                os.chmod(destination, 0o600)
                with destination.open("rb") as file:
                    os.fsync(file.fileno())
            record["backup_hashes"][relative] = file_hash(destination)
            atomic_json(self.record_path, record)
        if record["phase"] == "backup_started":
            record["phase"] = "backup_complete"
            atomic_json(self.record_path, record)
        if record["phase"] == "backup_complete":
            segment = self.directory / "sdk"
            segment.mkdir(parents=True, exist_ok=True, mode=0o700)
            record["segment"] = create_segment(segment, record)
            record["phase"] = "segment_created"
            atomic_json(self.record_path, record)
        check()
        if self._snapshot() != snapshot:
            raise MigrationBlocked("source changed before migration commit")
        record["phase"] = "committed"
        atomic_json(self.record_path, record)
        return record
