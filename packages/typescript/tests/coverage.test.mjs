import assert from 'node:assert/strict';
import {test} from 'node:test';

import {EnvironmentClient} from '../dist/client.js';
import {
  MISSING,
  SLOTS,
  buildTimeline,
  cellText,
  compact,
  describe,
  filterEvents,
  formatCost,
  formatTime,
  hasActivity,
  inheritedSentence,
  joinNames,
  nextRevision,
  participantOf,
  reconstructInherited,
  scalars,
  shortId,
  title,
  turnLabel,
  visible,
} from '../dist/timeline.js';

const event = (seq, revision, kind, payload = {}, audience = [], time = seq) => ({
  environment: 'environment', revision, seq, kind, payload, audience,
  ingested: time, event_time: null, previous: '', hash: '',
});

test('client validates endpoints, request limits, errors and every public request shape', async () => {
  for (const endpoint of [
    'http://supplier.example',
    'https://user@supplier.example',
    'https://supplier.example?token=bad',
    'https://supplier.example/#fragment',
  ]) assert.throws(() => new EnvironmentClient(endpoint, 'token'), /HTTPS endpoint/);
  assert.equal(new EnvironmentClient('http://localhost:8000', 'token', true).endpoint, 'http://localhost:8000');

  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({url, options});
    return new Response(JSON.stringify({url}), {status: 200});
  };
  try {
    const client = new EnvironmentClient('https://supplier.example/', 'old-token');
    client.setToken('new-token');
    await client.request('POST', '/direct', {value: 1}, 'operation');
    await client.list();
    await client.get('a/b');
    await client.create({synthetic: true}, 'create-operation');
    await client.observe('environment');
    await client.observe('environment', 'a/b');
    await client.submit('environment', {value: 1});
    await client.command('environment', 'lease', {owner: 'test'});
    await client.events('environment', 7);
    await client.agentWork('environment');
    await client.cancel('environment');
    await client.reports('environment');
    await client.compare(['one', 'two']);
    assert.equal(calls[0].options.headers.Authorization, 'Bearer new-token');
    assert.equal(calls[0].options.headers['X-Operation-ID'], 'operation');
    assert.equal(calls[2].url, 'https://supplier.example/v1/environments/a%2Fb');
    assert.match(calls[5].url, /participant=a%2Fb/);
    assert.equal(calls[8].options.body, undefined);
  } finally { globalThis.fetch = original; }

  const client = new EnvironmentClient('https://supplier.example', 'token');
  globalThis.fetch = async () => new Response('{}', {status: 403});
  await assert.rejects(client.list(), /HTTP 403/);
  globalThis.fetch = async () => new Response('x'.repeat(16777217), {status: 200});
  await assert.rejects(client.list(), /size limit/);
  globalThis.fetch = original;
});

test('client replay and follow have explicit cursor and abort behavior', async () => {
  const client = new EnvironmentClient('https://supplier.example', 'token');
  const pages = [
    {events: [event(1, 0, 'one')], cursor: 1},
    {events: [event(2, 0, 'two')], cursor: 2},
    {events: [], cursor: 2},
  ];
  client.events = async () => pages.shift();
  const replayed = [];
  for await (const item of client.replay('environment')) replayed.push(item.seq);
  assert.deepEqual(replayed, [1, 2]);

  const controller = new AbortController();
  client.events = async () => {
    controller.abort();
    return {events: [event(3, 0, 'followed')], cursor: 3};
  };
  const followed = [];
  for await (const item of client.follow('environment', controller.signal)) followed.push(item.seq);
  assert.deepEqual(followed, [3]);

  const waiting = new AbortController();
  client.events = async () => ({events: [], cursor: 0});
  setTimeout(() => waiting.abort(), 5);
  assert.deepEqual(await Array.fromAsync(client.follow('environment', waiting.signal)), []);
});

test('timeline helpers render sparse and detailed evidence safely', () => {
  assert.deepEqual(SLOTS, ['observation', 'attempted', 'executed']);
  assert.equal(MISSING.executed, 'not executed');
  assert.equal(shortId(null), '');
  assert.equal(shortId('123456789012345'), '123456789012');
  assert.equal(joinNames([]), '');
  assert.equal(joinNames(['a']), 'a');
  assert.equal(joinNames(['a', 'b', 'c']), 'a, b and c');
  assert.equal(title({participants: ['a'], parent: 'p'}), 'a (Branch)');
  assert.equal(title({environment: {id: 'synthetic'}}), 'synthetic (Original)');
  assert.equal(title({id: 'environment'}), 'environment (Original)');
  assert.equal(formatCost(1500000), '$1.50');
  assert.equal(formatCost(null), '$0.00');
  assert.equal(formatTime(null), '');
  assert.ok(formatTime(0));
  assert.equal(compact(undefined), '');
  assert.equal(compact({value: 1}), '{"value":1}');
  assert.equal(compact(1.23456789), '1.23457');
  assert.equal(compact(null), 'null');
  assert.equal(scalars(undefined), '');
  assert.equal(scalars('plain'), 'plain');
  assert.match(scalars({short: 1, long: 'x'.repeat(40)}, 20), /\.\.\.$/);
  assert.equal(visible(event(1, 0, 'x', {}, ['*']), 'a'), true);
  assert.equal(visible(event(1, 0, 'x', {}, ['b']), 'a'), false);
  assert.equal(filterEvents([event(1, 0, 'action.one', {}, ['a'])], 'a', 'action').length, 1);
  assert.equal(participantOf(event(1, 0, 'x', {participant: 'a'})), 'a');
  assert.equal(participantOf(event(1, 0, 'x', {action: {participant: 'b'}})), 'b');
  assert.equal(participantOf(event(1, 0, 'x')), null);
});

test('timeline groups transitions, shared data, duplicates and unknown participants', () => {
  const events = [
    event(1, 0, 'session.created', {experiment: {participants: [{id: 'a'}, {ignored: true}]}}),
    event(2, 0, 'observation.delivered', {participant: 'a', payload: {state: 1}}, ['a'], 3),
    event(3, 0, 'observation.delivered', {participant: 'a', payload: {state: 2}}, ['a'], 2),
    event(4, 0, 'observation.delivered', {payload: {}}, ['a']),
    event(5, 0, 'action.attempted', {action: {participant: 'a', payload: {value: 1}}, receipt: {status: 'blocked', reason: 'policy'}}, ['a']),
    event(6, 1, 'action.executed', {participant: 'a', outcome: {executed: true, value: 1}, reward: 1, reason: 'done'}, ['a']),
    event(7, 1, 'transition.committed', {state_hash: 'hash', terminated: true}),
    event(8, 1, 'synthetic.shared', {total: 1, nested: {hidden: true}}, ['*']),
    event(9, 1, 'checkpoint.committed', {id: 'checkpoint', exact_agents: true}),
    event(10, 1, 'artifact', {id: 'artifact'}),
    event(11, 1, 'report', {revision: 2}),
  ];
  const turns = buildTimeline(events, ['a', 'b']);
  assert.equal(turns.length, 2);
  assert.equal(turns[0].started, 1);
  assert.equal(turns[0].committed.revision, 1);
  assert.deepEqual(turns[0].shared, {total: 1});
  assert.equal(turns.reduce((count, turn) => count + turn.other.length, 0), 6);
  assert.equal(nextRevision(turns[0]), 1);
  assert.equal(turnLabel(turns[0]), 'Revision 0 to 1');
  assert.equal(hasActivity(turns[0]), true);
  assert.match(cellText(turns[0].participants.a.observation, 'observation'), /state 1/);
  assert.match(cellText(turns[0].participants.a.attempted, 'attempted'), /blocked: policy/);
  assert.match(cellText(turns[0].participants.a.executed, 'executed'), /reward 1.00 \(done\)/);
  assert.equal(describe(events[0]), 'Session created with participants a.');
  assert.match(describe(events[4]), /attempted/);
  assert.match(describe(events[5]), /executed/);
  assert.equal(describe(events[6]), 'State revision 1 committed.');
  assert.match(describe(events[8]), /with exact agent state/);
  assert.equal(describe(events[9]), 'Artifact artifact recorded.');
  assert.equal(describe(events[10]), 'Score report revision 2 recorded.');

  const open = buildTimeline([event(1, 0, 'custom', {})], ['a'])[0];
  assert.equal(nextRevision(open), null);
  assert.equal(turnLabel(open), 'Revision 0 (open)');
  assert.equal(hasActivity(open), false);
  assert.equal(describe(event(1, 0, 'custom', {})), 'custom recorded.');
  assert.equal(describe(event(1, 0, 'custom', {value: 1})), 'custom recorded: value 1.');
  assert.equal(describe(event(1, 0, 'session.created')), 'Session created.');
  assert.match(describe(event(1, 0, 'session.branched', {parent: 'p', checkpoint: 'c'})), /checkpoint c\.$/);
  assert.match(describe(event(1, 0, 'observation.delivered', {participant: 'a'})), /observed/);
});

test('inherited history validates chunks, identities, encodings and branch metadata', () => {
  const original = {environment: 'parent', seq: 1, hash: 'hash', audience: '["a"]', body: '{}'};
  const encoded = Buffer.from(JSON.stringify(original)).toString('base64');
  const chunk = event(1, 2, 'history.inherited.chunk', {
    environment: 'parent', seq: 1, hash: 'hash', parts: 1, part: 0,
    encoding: 'base64-json-v1', record_sha256: 'reviewed', data: encoded,
  }, ['a']);
  assert.deepEqual(reconstructInherited([chunk]), [{complete: true, record: original}]);
  const timeline = buildTimeline([
    chunk,
    event(2, 2, 'session.branched', {parent: 'parent', checkpoint: 'checkpoint', interventions: {total: 1}}),
  ], ['a']);
  assert.equal(timeline[0].inherited.checkpoint, 'checkpoint');
  assert.match(inheritedSentence(timeline[0].inherited), /checkpoint checkpoint/);
  assert.match(describe(timeline[0].other[0]), /interventions total 1/);
  const incomplete = reconstructInherited([{...chunk, payload: {...chunk.payload, parts: 2}}]);
  assert.equal(incomplete[0].complete, false);
  assert.match(inheritedSentence({count: 1, parent: 'parent', checkpoint: null, first_seq: 1, last_seq: 1, records: incomplete}), /more event pages/);
  assert.throws(() => reconstructInherited([{...chunk, payload: {...chunk.payload, encoding: 'unknown'}}]), /Unsupported/);
  const wrong = {...original, environment: 'wrong'};
  const wrongChunk = {...chunk, payload: {...chunk.payload, data: Buffer.from(JSON.stringify(wrong)).toString('base64')}};
  assert.throws(() => reconstructInherited([wrongChunk]), /identity mismatch/);
});
