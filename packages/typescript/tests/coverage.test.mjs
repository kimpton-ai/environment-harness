import assert from 'node:assert/strict';
import {test} from 'node:test';

import {EnvironmentClient, ServiceError} from '../dist/client.js';
import {
  MISSING,
  SLOTS,
  buildTimeline,
  buildTurnSeries,
  cellText,
  compact,
  describe,
  filterEvents,
  formatCompactCount,
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
  summarizeReports,
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
    await client.sessions({limit: 25, cursor: 'a'.repeat(32)});
    await client.session('a/b');
    await client.createExperiment({synthetic: true}, 'create-operation');
    await client.observe('environment', 'alice');
    await client.observe('environment', 'a/b');
    await client.submit('environment', 'alice', {value: 1});
    await client.command('environment', 'lease', {owner: 'test'});
    await client.evidence('environment', 7);
    await client.activityHierarchy();
    await client.activity(9);
    await client.experimentActivity('experiment/a', 10);
    await client.sessionActivity('environment/a', 11);
    await client.activityStream(12);
    await client.invocations('environment');
    await client.cancel('environment');
    await client.advance('environment');
    await client.credentials('environment', 'alice', 30);
    await client.scores('environment');
    await client.compare(['one', 'two']);
    await client.capabilities();
    await client.experiments({limit: 5});
    await client.experiment('a/b');
    await client.experimentSessions('a/b');
    await client.scenarioSets();
    await client.scenarioSet('a/b');
    await client.policies();
    await client.policy('a/b');
    await client.checkpoints('environment');
    await client.createCheckpoint('environment', true);
    await client.checkpoint('environment', 'cp');
    await client.branch('environment', {checkpoint: 'cp'});
    await client.snapshots({trajectory: 'a/b'});
    await client.snapshotRecords('a/b');
    await client.datasetRecords('a/b');
    await client.sources();
    await client.source('a/b');
    await client.sourceRecords('a/b');
    await client.trajectoryScores('a/b');
    await client.turnSeries('environment/a', {startTurn: 5, endTurn: 10, maxPoints: 200});
    assert.equal(calls[0].options.headers.Authorization, 'Bearer new-token');
    assert.equal(calls[0].options.headers['X-Operation-ID'], 'operation');
    assert.match(calls[1].url, /limit=25&cursor=a{32}$/);
    assert.equal(calls[2].url, 'https://supplier.example/v1/sessions/a%2Fb');
    assert.match(calls[5].url, /participants\/a%2Fb\/observation/);
    assert.equal(calls[8].options.body, undefined);
    assert.equal(calls[9].url, 'https://supplier.example/v1/activity/hierarchy');
    assert.match(calls[10].url, /after=9/);
    assert.match(calls[11].url, /experiments\/experiment%2Fa\/activity\?after=10/);
    assert.match(calls[12].url, /sessions\/environment%2Fa\/activity\?after=11/);
    assert.equal(calls[13].options.headers['Last-Event-ID'], '12');
    assert.ok(calls.some(call => JSON.parse(call.options.body ?? '{}').operation === 'advance'));
    // The participant is named by the route, never by the request body.
    assert.ok(calls.some(call => call.url.endsWith('/participants/alice/credentials') &&
      call.options.body === JSON.stringify({ttl: 30})));
    assert.match(calls.at(-1).url, /sessions\/environment%2Fa\/turn-series\?start_turn=5&max_points=200&end_turn=10/);
  } finally { globalThis.fetch = original; }

  const client = new EnvironmentClient('https://supplier.example', 'token');
  globalThis.fetch = async () => new Response(JSON.stringify({error: {
    code: 'forbidden', message: 'Environment unavailable', status: 403,
    request_id: 'a'.repeat(32), timestamp: '2026-09-21T12:00:00.000Z',
    details: [{field: 'environment', message: 'Unavailable', type: 'forbidden'}],
  }}), {
    status: 403, headers: {'Content-Type': 'application/json'},
  });
  await assert.rejects(client.sessions(), error => error instanceof ServiceError &&
    error.message === 'Environment service returned HTTP 403: Environment unavailable' &&
    error.code === 'forbidden' && error.status === 403 && error.requestId === 'a'.repeat(32) &&
    error.details[0].field === 'environment');
  globalThis.fetch = async () => new Response('private upstream detail', {status: 403});
  await assert.rejects(client.sessions(), error => error.message === 'Environment service returned HTTP 403');
  globalThis.fetch = async () => new Response('{', {status: 403, headers: {'Content-Type': 'application/json'}});
  await assert.rejects(client.sessions(), error => error.message === 'Environment service returned HTTP 403');
  globalThis.fetch = async () => new Response('x'.repeat(16777217), {status: 200});
  await assert.rejects(client.sessions(), /size limit/);
  globalThis.fetch = original;
});

test('client replay and follow have explicit cursor and abort behavior', async () => {
  const client = new EnvironmentClient('https://supplier.example', 'token');
  const pages = [
    {events: [event(1, 0, 'one')], cursor: 1},
    {events: [event(2, 0, 'two')], cursor: 2},
    {events: [], cursor: 2},
  ];
  client.evidence = async () => pages.shift();
  const replayed = [];
  for await (const item of client.replay('environment')) replayed.push(item.seq);
  assert.deepEqual(replayed, [1, 2]);

  const controller = new AbortController();
  client.evidence = async () => {
    controller.abort();
    return {events: [event(3, 0, 'followed')], cursor: 3};
  };
  const followed = [];
  for await (const item of client.follow('environment', controller.signal)) followed.push(item.seq);
  assert.deepEqual(followed, [3]);

  const waiting = new AbortController();
  client.evidence = async () => ({events: [], cursor: 0});
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
  assert.deepEqual(
    [0, 1, 999, 1000, 1100, 12500, 1100000].map(formatCompactCount),
    ['0', '1', '999', '1k', '1.1k', '13k', '1.1m'],
  );
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
  assert.equal(
    describe(event(1, 1, 'operation.intent', {
      id: 'inspect', participant: 'alice', request: {operation: 'synthetic.inspect-total'},
    })),
    'alice prepared synthetic.inspect-total as operation inspect.',
  );
  assert.equal(
    describe(event(2, 1, 'operation.receipt', {
      id: 'inspect', receipt: {operation_id: 'session:inspect', cost_micros: 0, meets_threshold: true},
    })),
    'Operation inspect completed: cost micros 0, meets threshold true.',
  );
});

test('turn series preserve rewards, public signals and sparse score revisions', () => {
  const events = [
    event(1, 0, 'observation.delivered', {participant: 'alice', payload: {total: 0}}, ['alice']),
    event(2, 1, 'action.executed', {participant: 'alice', outcome: {value: 1}, reward: 1}, ['alice']),
    event(3, 1, 'transition.committed', {state_hash: 'one'}),
    event(4, 1, 'synthetic.total', {total: 1}, ['*']),
    event(5, 1, 'report', {revision: 1, hash: 'report-one'}),
    event(6, 2, 'action.executed', {participant: 'alice', outcome: {value: 1}, reward: 2}, ['alice']),
    event(7, 2, 'transition.committed', {state_hash: 'two'}),
    event(8, 2, 'synthetic.total', {total: 3}, ['*']),
    event(9, 3, 'action.executed', {participant: 'alice', outcome: {value: 1}, reward: 3}, ['alice']),
    event(10, 3, 'transition.committed', {state_hash: 'three'}),
    event(11, 3, 'synthetic.total', {total: 6}, ['*']),
    event(12, 3, 'report', {revision: 2, hash: 'report-two'}),
  ];
  const report = (revision, value) => ({
    environment: 'environment', revision, hash: `report-${revision}`,
    report: {scorer: 'control', version: '1', kind: 'deterministic', evidence_cursor: revision === 1 ? 4 : 11,
      metrics: {quality: value}, metric_definitions: {quality: {id: 'quality', version: '1', unit: 'score'}},
      findings: [], rewards: {}, uncertainty: '', provenance: {}},
  });
  const projection = buildTurnSeries(events, ['alice'], [report(1, 0.25), report(2, 0.75)]);
  assert.deepEqual(
    projection.series.find(series => series.id === 'reward:alice').points,
    [{turn: 1, revision: 1, value: 1}, {turn: 2, revision: 2, value: 2}, {turn: 3, revision: 3, value: 3}],
  );
  assert.deepEqual(
    projection.series.find(series => series.id === 'signal:synthetic.total:total').points,
    [{turn: 1, revision: 1, value: 1}, {turn: 2, revision: 2, value: 3}, {turn: 3, revision: 3, value: 6}],
  );
  assert.deepEqual(
    projection.series.find(series => series.id === 'metric:control:1:quality').points,
    [{turn: 1, revision: 1, value: 0.25}, {turn: 3, revision: 3, value: 0.75}],
    'score series preserve the unscored turn as a visible gap',
  );
});

test('turn series expose activity, missing participants and attributed findings', () => {
  const events = [
    event(1, 0, 'observation.delivered', {participant: 'alice', payload: {}}, ['alice']),
    event(2, 0, 'action.attempted', {action: {participant: 'alice', payload: {}}, receipt: {status: 'blocked'}}, ['alice']),
    event(3, 0, 'action.attempted', {action: {participant: 'alice', payload: {}}, receipt: {status: 'blocked'}}, ['alice']),
    event(4, 1, 'transition.committed', {state_hash: 'one', missing: ['bob']}),
    event(5, 1, 'observation.delivered', {participant: 'alice', payload: {}}, ['alice']),
    event(6, 1, 'action.attempted', {action: {participant: 'alice', payload: {}}, receipt: {status: 'accepted'}}, ['alice']),
    event(7, 2, 'action.executed', {action_id: 'action', participant: 'alice', outcome: {}}, ['alice']),
    event(8, 2, 'transition.committed', {state_hash: 'two', missing: []}),
    event(9, 2, 'report', {revision: 1, hash: 'report'}),
  ];
  const reports = [{environment: 'environment', revision: 1, hash: 'report', report: {
    scorer: 'control', version: '1', kind: 'deterministic', evidence_cursor: 8, metrics: {}, metric_definitions: {}, rewards: {},
    findings: [{rule: 'unsafe', participant: 'alice', observation_id: 'observation', action_id: 'action', outcome_event: 7,
      consequence_events: [], category: 'harm', status: 'executed', judgment: 'recorded', uncertainty: ''}],
    uncertainty: '', provenance: {},
  }}];
  const projection = buildTurnSeries(events, ['alice', 'bob'], reports);
  assert.deepEqual(projection.series.find(series => series.id === 'activity:observed').points.map(point => point.value), [1, 1]);
  assert.deepEqual(projection.series.find(series => series.id === 'activity:attempted').points.map(point => point.value), [2, 1]);
  assert.deepEqual(projection.series.find(series => series.id === 'activity:executed').points.map(point => point.value), [0, 1]);
  assert.deepEqual(projection.series.find(series => series.id === 'activity:blocked').points.map(point => point.value), [2, 0]);
  assert.deepEqual(projection.series.find(series => series.id === 'activity:missing').points.map(point => point.value), [1, 0]);
  assert.deepEqual(projection.series.find(series => series.id === 'finding:harm').points, [{turn: 2, revision: 2, value: 1}]);
});

test('report summaries expose metric change and deduplicate findings', () => {
  const events = [
    event(1, 0, 'observation.delivered', {participant: 'alice'}, ['alice']),
    event(2, 1, 'action.executed', {participant: 'alice'}, ['alice']),
    event(3, 1, 'transition.committed', {state_hash: 'one'}),
    event(4, 1, 'report', {revision: 1, hash: 'one'}),
    event(5, 1, 'observation.delivered', {participant: 'alice'}, ['alice']),
    event(6, 1, 'action.attempted', {action: {participant: 'alice'}}, ['alice']),
    event(7, 2, 'action.executed', {participant: 'alice'}, ['alice']),
    event(8, 2, 'transition.committed', {state_hash: 'two'}),
    event(9, 2, 'report', {revision: 2, hash: 'two'}),
  ];
  const finding = {rule: 'unsafe', participant: 'alice', observation_id: 'observation', action_id: 'action',
    outcome_event: 7, consequence_events: [], category: 'harm', status: 'executed', judgment: 'Unsafe action executed.', uncertainty: ''};
  const report = (revision, value) => ({environment: 'environment', revision, hash: String(revision), report: {
    scorer: 'control', version: '1', kind: 'deterministic', evidence_cursor: revision === 1 ? 3 : 8,
    metrics: {quality: value}, metric_definitions: {quality: {id: 'quality', version: '1', unit: 'score'}},
    findings: [finding], rewards: {}, uncertainty: 'Synthetic only.', provenance: {},
  }});
  const summary = summarizeReports(events, ['alice'], [report(1, 0.25), report(2, 0.75)]);
  assert.deepEqual(summary.metrics, [{
    id: 'metric:control:1:quality', label: 'Quality', scorer: 'control', version: '1', unit: 'score',
    first: 0.25, latest: 0.75, change: 0.5, samples: 2,
  }]);
  assert.equal(summary.latestTurn, 2);
  assert.deepEqual(summary.scorers, ['control@1']);
  assert.equal(summary.findings.length, 1);
  assert.equal(summary.findings[0].turn, 2);
  assert.deepEqual(summary.uncertainties, ['Synthetic only.']);
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
