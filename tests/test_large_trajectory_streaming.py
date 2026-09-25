"""Bounded-memory streaming and restart-safe pagination for large trajectories.

The default profile is a fast, deterministic subset. Set
``ENVIRONMENT_HARNESS_LARGE_TRAJECTORY=1`` to run the full 100,000-record,
roughly 100 MiB profile that the scheduled job uses; the assertions are
identical.

The allocation bound is a deterministic Python-allocation proxy measured with
``tracemalloc``. It does not claim to bound SQLite, driver, or other
C-extension RSS; the scheduled benchmark records process RSS and throughput
separately.
"""

from __future__ import annotations

import os
import tracemalloc

import pytest

from environment_harness import EvidenceStore
from environment_harness.access import trusted_local
from environment_harness.trajectories import (
    SourceRecord,
    SourceRegistration,
    SourceStatusUpdate,
    TrajectoryRepository,
)

FULL_PROFILE = os.environ.get("ENVIRONMENT_HARNESS_LARGE_TRAJECTORY") == "1"
RECORD_COUNT = 100_000 if FULL_PROFILE else 2_000
#: Roughly 1 KiB of payload per record, so the full profile is about 100 MiB.
PAYLOAD = "x" * 1000
PAGE_SIZE = 1_000
ALLOCATION_CEILING = 32 * 1024 * 1024


def _ingest(store, count):
    repository = TrajectoryRepository(store)
    access = trusted_local("large")
    source = repository.register_source(
        SourceRegistration(
            namespace="com.example.large",
            run_id="run-1",
            schema_version="large.v1",
            environment={"id": "large", "version": "1"},
            participants=("alice",),
            purpose="evaluation",
        ),
        access,
    )
    previous = "0" * 64
    batch = []
    for index in range(1, count + 1):
        record = SourceRecord.create(
            id=f"record-{index}",
            position=str(index),
            previous_hash=previous,
            type="com.example.large.frame",
            segment="segment-1",
            participant="alice",
            revision=index,
            time={
                "wallTime": "2026-09-22T15:00:00Z",
                "native": [{"clock": "simulator.frame", "value": index}],
            },
            data={"payload": PAYLOAD},
            audience=("*",),
        )
        previous = record.source_hash
        batch.append(record)
        if len(batch) == PAGE_SIZE:
            repository.ingest(source.id, tuple(batch), access)
            batch.clear()
    if batch:
        repository.ingest(source.id, tuple(batch), access)
    return repository, access, source.id


@pytest.mark.skipif(
    not FULL_PROFILE and os.environ.get("ENVIRONMENT_HARNESS_SKIP_LARGE") == "1",
    reason="large-trajectory profile explicitly skipped",
)
def test_large_trajectory_streams_within_a_bounded_allocation_budget(tmp_path):
    store = EvidenceStore(tmp_path)
    repository, access, source = _ingest(store, RECORD_COUNT)

    trajectory = repository.get(source, access)
    assert trajectory.status.record_count == RECORD_COUNT
    assert trajectory.status.sequence_end == RECORD_COUNT

    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    seen = 0
    highest = 0
    identities: set[str] = set()
    # The consumer reads incrementally and never holds the whole trajectory.
    for record in repository.stream_records(source, access, page_size=PAGE_SIZE):
        seen += 1
        assert record.sequence > highest
        highest = record.sequence
        identities.add(record.id)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    assert seen == RECORD_COUNT
    assert len(identities) == RECORD_COUNT
    assert peak - baseline < ALLOCATION_CEILING, peak - baseline


def test_pagination_restart_neither_omits_nor_duplicates_records(tmp_path):
    store = EvidenceStore(tmp_path)
    repository, access, source = _ingest(store, min(RECORD_COUNT, 3_000))
    expected = repository.get(source, access).status.record_count

    # Read half the stream, discard the reader, and resume from its cursor.
    cursor = 0
    collected: list[int] = []
    while len(collected) < expected // 2:
        page = repository.records_page(source, access, after=cursor, limit=PAGE_SIZE)
        assert len(page.records) <= PAGE_SIZE
        collected.extend(record.sequence for record in page.records)
        cursor = page.cursor
    resumed: list[int] = []
    while True:
        page = repository.records_page(source, access, after=cursor, limit=PAGE_SIZE)
        resumed.extend(record.sequence for record in page.records)
        cursor = page.cursor
        if not page.has_more:
            break

    combined = collected + resumed
    assert combined == sorted(combined)
    assert len(combined) == len(set(combined)) == expected

    # Re-reading an already consumed cursor returns the same page.
    first = repository.records_page(source, access, after=0, limit=PAGE_SIZE)
    again = repository.records_page(source, access, after=0, limit=PAGE_SIZE)
    assert [record.id for record in first.records] == [record.id for record in again.records]

    with pytest.raises(ValueError, match="page size"):
        list(repository.stream_records(source, access, page_size=0))
    with pytest.raises(ValueError, match="record page"):
        repository.records_page(source, access, after=0, limit=1001)


def test_snapshot_export_streams_the_same_ordered_records(tmp_path):
    store = EvidenceStore(tmp_path)
    repository, access, source = _ingest(store, min(RECORD_COUNT, 2_000))
    repository.update_source_status(
        source,
        SourceStatusUpdate(
            collection_state="complete",
            execution_state="completed",
            termination={"terminated": True, "truncated": False, "reason": "goal"},
            verified_outcome={"state": "success", "evidence": ()},
            terminal_position=str(min(RECORD_COUNT, 2_000)),
            terminal_hash=repository.source_status(source, access).acknowledged_hash,
            backlog=0,
        ),
        access,
    )
    snapshot = repository.freeze(source, access)
    streamed = [record.id for record in repository.stream_records(source, access)]

    rows = list(repository.export_snapshot(snapshot.metadata.id, access))
    assert rows[0]["snapshot"]["status"]["recordCount"] == len(streamed)
    assert [row["id"] for row in rows[1:]] == streamed

    # Re-exporting the same snapshot reproduces canonically equal contents.
    assert list(repository.export_snapshot(snapshot.metadata.id, access)) == rows
    assert repository.freeze(source, access).status.snapshot_digest == snapshot.status.snapshot_digest
