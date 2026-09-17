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
    return new Response(JSON.stringify(url.endsWith('/v1/compare')
      ? {environments: [], metrics: {}, metric_groups: [], warnings: ['synthetic'], uncertainty: '', design: ''}
      : {status: 'cancelled', unresolved_agent_work: ['work'], unresolved_operations: []}), {status: 200});
  };
  try {
    const client = new EnvironmentClient('https://supplier.example', 'synthetic-token');
    assert.equal((await client.cancel('a/b')).status, 'cancelled');
    assert.equal(calls[0].url, 'https://supplier.example/v1/environments/a%2Fb/commands');
    assert.deepEqual(JSON.parse(calls[0].options.body), {operation: 'cancel', arguments: {}});
    assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-token');
    assert.deepEqual((await client.compare(['one', 'two'])).warnings, ['synthetic']);
    assert.deepEqual(JSON.parse(calls[1].options.body), {environments: ['one', 'two']});
  } finally {globalThis.fetch = original;}
});
