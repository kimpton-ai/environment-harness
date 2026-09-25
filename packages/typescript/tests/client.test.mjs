import assert from 'node:assert/strict';
import {test} from 'node:test';
import {EnvironmentClient} from '../dist/client.js';
import {buildTimeline, reconstructInherited} from '../dist/timeline.js';
import {createHash} from 'node:crypto';

function fixture() {
  const record = {environment: 'parent', seq: 2, hash: 'original-hash', body: JSON.stringify({text: '💡'.repeat(500)}), audience: '["a"]'};
  const raw = JSON.stringify(record), encoded = Buffer.from(raw).toString('base64');
  const parts = Math.ceil(encoded.length / 1000);
  const events = Array.from({length: parts}, (_, part) => ({
    environment: 'child', revision: 0, seq: part + 1, kind: 'history.inherited.chunk', audience: ['a'],
    ingested: 0, event_time: null, previous: '', hash: '',
    payload: {environment: record.environment, seq: record.seq, hash: record.hash, encoding: 'base64-json-v1',
      record_sha256: createHash('sha256').update(raw).digest('hex'), part, parts, data: encoded.slice(part * 1000, (part + 1) * 1000)},
  }));
  return {record, events};
}

test('chunked records reconstruct exactly and count once', () => {
  const {record, events} = fixture();
  assert.deepEqual(reconstructInherited(events), [{complete: true, record}]);
  const timeline = buildTimeline(events, ['a']);
  assert.equal(timeline[0].inherited.count, 1);
  assert.deepEqual(timeline[0].inherited.records, [{complete: true, record}]);
  assert.equal(buildTimeline(events.slice(1), ['a'])[0].inherited.count, 1);
  assert.equal(reconstructInherited(events.slice(1))[0].complete, false);
  assert.throws(() => reconstructInherited([...events, events[0]]), /chunk sequence/);
});

test('legacy inherited payloads are still readable', () => {
  const {record, events} = fixture();
  const legacy = {...events[0], kind: 'history.inherited', payload: record};
  assert.deepEqual(reconstructInherited([legacy]), [{complete: true, record}]);
  assert.equal(buildTimeline([legacy], ['a'])[0].inherited.count, 1);
});

test('cancel is lease-independent and comparison forwards the compatible response', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    return new Response(JSON.stringify(url.endsWith('/v1/comparisons')
      ? {environments: [], metrics: {}, metric_groups: [], warnings: ['synthetic'], uncertainty: '', design: ''}
      : {operation: 'cancel', accepted: true, result: {status: 'cancelled', unresolved_agent_work: ['work'], unresolved_operations: []}}), {status: 200});
  };
  try {
    const client = new EnvironmentClient('https://supplier.example', 'synthetic-token');
    assert.equal((await client.cancel('a/b')).status, 'cancelled');
    assert.equal(calls[0].url, 'https://supplier.example/v1/sessions/a%2Fb/commands');
    assert.deepEqual(JSON.parse(calls[0].options.body), {operation: 'cancel', arguments: {}});
    assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-token');
    assert.deepEqual((await client.compare(['one', 'two'])).warnings, ['synthetic']);
    assert.deepEqual(JSON.parse(calls[1].options.body), {sessions: ['one', 'two']});
  } finally {globalThis.fetch = original;}
});

test('trajectory client methods encode resource identities and management requests', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    return new Response('{}', {status: 200});
  };
  try {
    const client = new EnvironmentClient('https://supplier.example', 'synthetic-token');
    await client.trajectories({limit: 25, cursor: 'source-a'});
    await client.trajectory('a/b');
    await client.trajectoryRecords('a/b', {after: 12, limit: 50});
    await client.registerSource({namespace: 'example'});
    await client.ingestSource('a/b', {records: []});
    await client.sourceStatus('a/b');
    await client.reportSourceStatus('a/b', {collection_state: 'current'});
    await client.freezeTrajectory('a/b');
    await client.trajectorySnapshots('a/b');
    await client.snapshot('a/b');
    await client.freezeDataset('training', ['a/b']);
    await client.dataset('a/b');
    await client.trainingRun('a/b');
    await client.datasets({limit: 25});
    await client.trainingRuns({dataset: 'a/b', limit: 25});

    assert.equal(calls[0].url, 'https://supplier.example/v1/trajectories?limit=25&cursor=source-a');
    assert.equal(calls[1].url, 'https://supplier.example/v1/trajectories/a%2Fb');
    assert.equal(calls[2].url, 'https://supplier.example/v1/trajectories/a%2Fb/records?after=12&limit=50');
    assert.equal(calls[3].url, 'https://supplier.example/v1/sources');
    assert.equal(calls[4].url, 'https://supplier.example/v1/sources/a%2Fb/records');
    assert.equal(calls[5].url, 'https://supplier.example/v1/sources/a%2Fb/status');
    // Declaring collection health is a status report, not an idempotent PUT.
    assert.equal(calls[6].options.method, 'POST');
    assert.equal(calls[6].url, 'https://supplier.example/v1/sources/a%2Fb/status-reports');
    assert.equal(calls[7].url, 'https://supplier.example/v1/trajectories/a%2Fb/snapshots');
    assert.equal(calls[8].url, 'https://supplier.example/v1/trajectories/a%2Fb/snapshots');
    assert.equal(calls[9].url, 'https://supplier.example/v1/snapshots/a%2Fb');
    assert.equal(calls[10].url, 'https://supplier.example/v1/datasets');
    assert.equal(calls[11].url, 'https://supplier.example/v1/datasets/a%2Fb');
    assert.equal(calls[12].url, 'https://supplier.example/v1/training-runs/a%2Fb');
    assert.equal(calls[13].url, 'https://supplier.example/v1/datasets?limit=25');
    assert.equal(calls[14].url, 'https://supplier.example/v1/training-runs?dataset=a%2Fb&limit=25');
  } finally {globalThis.fetch = original;}
});

test('trajectory exports stream JSONL incrementally', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    return new Response('{"row":1}\n\n{"row":2}\n', {status:200, headers:{'content-type':'application/x-ndjson'}});
  };
  try {
    const client = new EnvironmentClient('https://supplier.example', 'synthetic-token');
    const snapshot = [], dataset = [];
    for await (const row of client.streamSnapshotRecords('a/b')) snapshot.push(row);
    for await (const row of client.streamDatasetRecords('c/d')) dataset.push(row);
    assert.deepEqual(snapshot, [{row:1}, {row:2}]);
    assert.deepEqual(dataset, [{row:1}, {row:2}]);
    // Content negotiation replaces the /export verb path.
    assert.equal(calls[0].url, 'https://supplier.example/v1/snapshots/a%2Fb/records');
    assert.equal(calls[1].url, 'https://supplier.example/v1/datasets/c%2Fd/records');
    assert.equal(calls[0].options.headers.Accept, 'application/x-ndjson');
  } finally {globalThis.fetch = original;}
});
