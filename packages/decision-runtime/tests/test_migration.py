import sqlite3

import pytest

from environment_harness_decisions.migration import MigrationBlocked, MigrationTransaction, sqlite_backup


def _wal_database(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
    db.execute("INSERT INTO evidence VALUES ('committed-in-wal')")
    db.commit()
    db.close()


def test_sqlite_backup_copies_committed_wal_state(tmp_path):
    source = tmp_path / "run.sqlite"
    destination = tmp_path / "backup.sqlite"
    _wal_database(source)
    sqlite_backup(source, destination)
    db = sqlite3.connect(destination)
    assert db.execute("SELECT value FROM evidence").fetchone()[0] == "committed-in-wal"
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()


def test_migration_report_is_read_only_and_apply_resumes_after_interruption(tmp_path):
    source = tmp_path / "run.sqlite"
    _wal_database(source)
    transaction = MigrationTransaction(
        tmp_path, version="runtime.v1", source_identity={"run": "legacy"},
        destination_identity={"sdk": "decisions"}, source_files=[source],
    )
    before = transaction.report()
    assert before["phase"] == "not_started"
    assert not (tmp_path / ".decision-runtime").exists() or transaction.report() == before

    calls = []
    def check():
        calls.append("check")
    def interrupted(segment, record):
        calls.append("segment")
        raise RuntimeError("migration interrupted")

    with pytest.raises(RuntimeError):
        transaction.apply(check=check, create_segment=interrupted)
    mid = transaction.report()
    assert mid["phase"] == "backup_complete"
    assert mid["original_retained"] is True
    original_bytes = source.read_bytes()

    def create_segment(segment, record):
        (segment / "segment.json").write_text('{"linked": true}\n')
        return {"identity": "sdk-segment", "source": record["source_identity"]}

    result = transaction.apply(check=check, create_segment=create_segment)
    assert result["phase"] == "committed"
    assert source.read_bytes() == original_bytes
    assert calls.count("segment") == 1
    assert transaction.report()["read_only"] is True


def test_migration_blocks_when_original_changes_after_backup(tmp_path):
    source = tmp_path / "run.sqlite"
    _wal_database(source)
    transaction = MigrationTransaction(
        tmp_path, version="runtime.v1", source_identity={"run": "legacy"},
        destination_identity={"sdk": "decisions"}, source_files=[source],
    )
    transaction.apply(check=lambda: None, create_segment=lambda segment, record: {"id": "sdk"})
    with source.open("ab") as file:
        file.write(b"changed")
    with pytest.raises(MigrationBlocked):
        transaction.apply(check=lambda: None, create_segment=lambda segment, record: {"id": "sdk"})
