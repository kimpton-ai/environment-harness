"""Score semantics and lossless inheritance across saved sessions."""

import copy
import time

import pytest

from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.contracts import RunPolicy, ScoreReport
from environment_harness.errors import Conflict
from environment_harness.evaluation import compare
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.history import reconstruct_inherited
from environment_harness.presentation import build_timeline, render_comparison, render_timeline
from environment_harness.store import digest, encode


def setup(path, limit=4096):
    store = EvidenceStore(path)
    env = SyntheticEnvironment()
    session = EnvironmentSession(store, env)
    who = Principal(tenant='local', subject='researcher', role='researcher')
    spec = ExperimentSpec(environment=env.spec,
        participants=tuple(AgentSpec(id=p, implementation=SyntheticAgent.implementation, policy_version='1') for p in ('a', 'b')),
        scoring_versions=('accuracy@1', 'accuracy@2', 'cost@1'), policy=RunPolicy(max_event_bytes=limit))
    environment = session.create(spec, who)['id']
    return store, session, who, spec, environment


def report(store, environment, who, *, scorer='accuracy', version='1', metric='accuracy', value=0.5,
           unit='fraction', definition_version='1', kind='deterministic'):
    definitions = {} if unit is None else {metric: {'id': metric, 'version': definition_version, 'unit': unit}}
    return store.report(environment, who, ScoreReport(scorer=scorer, version=version, kind=kind,
        evidence_cursor=1, metrics={metric: value}, metric_definitions=definitions,
        uncertainty='synthetic only', provenance={'synthetic': True}))


@pytest.mark.parametrize('difference', ['unit', 'scorer_version', 'definition_version', 'kind', 'participant', 'split', 'policy'])
def test_incompatible_scores_never_share_statistics(tmp_path, difference):
    store, session, who, spec, first = setup(tmp_path)
    other_spec = spec
    if difference == 'participant':
        other_spec = spec.model_copy(update={'participants': tuple(p.model_copy(update={'policy_version': '2'}) for p in spec.participants)})
    elif difference == 'split':
        other_spec = spec.model_copy(update={'scenario': 'training', 'split': 'training', 'purpose': 'training'})
    elif difference == 'policy':
        other_spec = spec.model_copy(update={'policy': spec.policy.model_copy(update={'max_turns': 5})})
    second = session.create(other_spec, who)['id']
    report(store, first, who)
    args = {'value': 50}
    if difference == 'unit':
        args['unit'] = 'percent'
    elif difference == 'scorer_version':
        args['version'] = '2'
    elif difference == 'definition_version':
        args['definition_version'] = '2'
    elif difference == 'kind':
        args['kind'] = 'human'
    report(store, second, who, **args)
    result = compare(store, [first, second], who)
    assert 'accuracy' not in result['metrics']
    assert len(result['metric_groups']) == 2
    assert {g['summary']['mean_of_lineage_means'] for g in result['metric_groups']} == {0.5, 50}
    assert result['warnings']
    assert 'shown separately' in render_comparison(result)


def test_latest_per_scorer_legacy_and_missing_values(tmp_path):
    store, session, who, spec, first = setup(tmp_path)
    second = session.create(spec, who)['id']
    report(store, first, who, value=0.1)
    selected = report(store, first, who, value=0.9)
    report(store, first, who, scorer='cost', metric='cost', value=3, unit='micros')
    report(store, second, who, value=0.8, unit=None)
    result = compare(store, [first, second, first], who)
    assert len(result['environments']) == 2
    assert len(result['environments'][0]['selected_reports']) == 2
    groups = result['metric_groups']
    accuracy = next(g for g in groups if g['metric'] == 'accuracy' and g['definition'])
    assert accuracy['values'][0]['report_revision'] == selected['revision']
    assert accuracy['summary']['mean_of_lineage_means'] == 0.9
    assert accuracy['missing_environments'] == 1 and accuracy['incomplete_environments'] == 2
    legacy = next(g for g in groups if g['definition'] is None)
    assert legacy['summary'] is None and legacy['values'][0]['value'] == 0.8
    assert 'accuracy' not in result['metrics']
    assert result['metrics']['cost']['mean_of_lineage_means'] == 3
    assert 'Raw values only' in render_comparison(result)


def test_single_lineage_compatible_metrics_keep_legacy_response(tmp_path):
    store, session, who, _, parent = setup(tmp_path)
    lease = session.lease(parent, who, 'checkpoint')
    cp = session.checkpoint(parent, who, lease)
    child = session.branch(parent, who, cp['id'], {'total': 20})['id']
    report(store, parent, who, value=0)
    report(store, child, who, value=1)
    result = compare(store, [parent, child], who)
    assert result['metrics']['accuracy'] == {
        'mean_of_lineage_means': 0.5, 'independent_lineages': 1, 'standard_error': None,
    }
    assert all(record['latest_report'] is not None for record in result['environments'])


@pytest.mark.parametrize('text', ['a' * 4000, '\\"' * 990, '💡' * 330])
def test_large_history_is_lossless_private_and_does_not_grow_on_rebranch(tmp_path, text):
    store, session, who, _, parent = setup(tmp_path)
    payload = {'text': text}
    assert len(encode(payload)) <= 4096
    with store.transaction() as db:
        original = store.append(db, parent, 0, 'private.large', payload, ('a',), event_time=0)
        original_row = dict(db.execute('SELECT * FROM events WHERE environment=? AND seq=?', (parent, original['seq'])).fetchone())
    lease = session.lease(parent, who, 'checkpoint')
    saved = session.checkpoint(parent, who, lease)
    child = session.branch(parent, who, saved['id'])['id']
    history = list(store.replay(child, who))
    chunks = [e for e in history if e['kind'] == 'history.inherited.chunk']
    assert len(chunks) >= 2
    assert all(len(encode(e['payload'])) <= 4096 for e in history)
    restored = list(reconstruct_inherited(history))
    assert next(r['record'] for r in restored if r['record']['kind'] == 'private.large') == original_row
    assert store.verify(child, who)['events'] == len(history)
    alice = Principal(tenant='local', subject='a', role='agent', environment=child, participant='a')
    bob = alice.model_copy(update={'subject': 'b', 'participant': 'b'})
    assert any(r['record']['kind'] == 'private.large' for r in reconstruct_inherited(store.replay(child, alice)))
    assert not any(r['record']['kind'] == 'private.large' for r in reconstruct_inherited(store.replay(child, bob)))
    turns = build_timeline(history, ('a', 'b'))
    assert turns[0]['inherited']['count'] == 2
    assert 'private.large' in render_timeline(turns, verbose=True)
    for _ in range(3):
        lease = session.lease(child, who, 'checkpoint')
        saved = session.checkpoint(child, who, lease)
        child = session.branch(child, who, saved['id'])['id']
        history = list(store.replay(child, who))
        descendant_chunks = [e['payload'] for e in history if e['kind'] == 'history.inherited.chunk']
        assert descendant_chunks == [e['payload'] for e in chunks]
        assert store.verify(child, who)['events'] == len(history)
        assert next(r['record'] for r in reconstruct_inherited(history) if r['record']['kind'] == 'private.large') == original_row
    damaged = copy.deepcopy(chunks)
    damaged[0]['payload']['data'] = 'AAAA' + damaged[0]['payload']['data'][4:]
    with pytest.raises(Conflict, match='integrity'):
        list(reconstruct_inherited(damaged))
    with pytest.raises(Conflict, match='incomplete'):
        list(reconstruct_inherited(chunks[1:]))
    assert list(reconstruct_inherited(chunks[1:], strict=False))[0]['complete'] is False


def test_legacy_integral_timestamp_and_reports_remain_readable(tmp_path):
    store, session, who, _, environment = setup(tmp_path)
    with store.transaction() as db:
        last = db.execute('SELECT hash FROM events WHERE environment=? ORDER BY seq DESC', (environment,)).fetchone()[0]
        # This is the original writer's integer timestamp representation, before REAL conversion.
        event = dict(environment=environment, seq=2, revision=0, kind='legacy.feed', payload={}, audience=['*'],
                     event_time=0, ingested=time.time(), previous=last)
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (environment, 2, 0, event['kind'], '{}', '["*"]', 0, event['ingested'], last, digest(event)))
        body = dict(scorer='accuracy', version='1', kind='deterministic', evidence_cursor=2,
                    metrics={'accuracy': 0.5}, findings=[], rewards={}, uncertainty='unknown', provenance={})
        db.execute('INSERT INTO reports VALUES (?,?,?,?)', (environment, 1, encode(body), digest({
            'environment': environment, 'revision': 1, 'report': body})))
    reopened = EvidenceStore(tmp_path)
    assert reopened.verify(environment, who)['events'] == 2
    assert reopened.events(environment, who)[1]['event_time'] == 0
    assert ScoreReport.model_validate(body).metric_definitions == {}
    result = compare(reopened, [environment], who)
    assert result['environments'][0]['latest_report']['report'] == body
    assert result['metrics'] == {} and result['warnings']
