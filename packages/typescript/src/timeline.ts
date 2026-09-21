// Turn grouping and one-line summaries for recorded evidence.
// Mirrors src/environment_harness/presentation.py: same rules, same field names.
// A turn is keyed by the revision an action was taken from. The transition that commits
// the next revision, the executed outcomes and the shared-state broadcast that follows
// carry revision + 1 and are assigned to the previous turn.
import type {EvidenceEvent, Finding, Json, ReportEnvelope} from './types.js';

export type Slot = 'observation' | 'attempted' | 'executed';
export const SLOTS: Slot[] = ['observation', 'attempted', 'executed'];
export type Slots = Record<Slot, EvidenceEvent | null>;
export interface Reconstructed {complete: boolean; record?: Record<string, Json>; environment?: string; seq?: number;}
export interface Inherited {records?: Reconstructed[];count: number; parent: string | null; checkpoint: string | null; first_seq: number; last_seq: number;}
export interface Committed {revision: number; seq: number; state_hash: string | null; terminated: boolean; truncated: boolean;}
export interface Turn {
  revision: number; committed: Committed | null; started: number | null; shared: Record<string, Json>;
  participants: Record<string, Slots>; inherited: Inherited | null; other: EvidenceEvent[]; seqs: number[];
}
export interface TurnSeriesPoint {turn: number; revision: number; value: number;}
export interface TurnSeries {
  id: string; label: string; kind: 'reward' | 'metric' | 'signal' | 'activity' | 'finding'; unit: string | null;
  participant: string | null; points: TurnSeriesPoint[];
}
export interface TurnSeriesProjection {series: TurnSeries[];}
export interface EvaluationMetric {
  id: string; label: string; scorer: string; version: string; unit: string | null;
  first: Json; latest: Json; change: number | null; samples: number;
}
export interface EvaluationFinding extends Finding {scorer: string; version: string; turn: number | null;}
export interface EvaluationSummary {
  metrics: EvaluationMetric[]; findings: EvaluationFinding[]; scorers: string[];
  latestTurn: number | null; latestCursor: number | null; uncertainties: string[];
}
export interface Titled {id?: string; participants?: string[]; parent?: string | null; environment?: Record<string, Json>;}

const SLOT_KINDS: Record<string, Slot> = {'observation.delivered': 'observation', 'action.attempted': 'attempted', 'action.executed': 'executed'};
const OUTCOME_KINDS = new Set(['transition.committed', 'action.executed']);
const LONG_TEXT = 24;
export const MISSING: Record<Slot, string> = {observation: 'no observation', attempted: 'no action yet', executed: 'not executed'};

const record = (value: Json | undefined): Record<string, Json> | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? value : null;

export const shortId = (value: unknown) => String(value ?? '').slice(0, 12);

export function joinNames(names: string[]) {
  if (names.length <= 1) return names.join('');
  return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
}

export function title(item: Titled) {
  const role = item.parent ? 'Branch' : 'Original';
  if (item.participants?.length) return `${item.participants.join(', ')} (${role})`;
  const id = record(item.environment)?.id;
  return `${typeof id === 'string' ? id : shortId(item.id)} (${role})`;
}

export function formatCost(micros: number | null | undefined) {
  const [whole, fraction] = ((micros ?? 0) / 1e6).toFixed(6).split('.');
  return `$${whole}.${fraction.replace(/0+$/, '').padEnd(2, '0')}`;
}

export function formatCompactCount(value: number) {
  const count = Math.max(0, Math.trunc(value));
  if (count < 1_000) return String(count);
  const units = [[1_000_000_000, 'b'], [1_000_000, 'm'], [1_000, 'k']] as const;
  for (const [scale, suffix] of units) {
    if (count < scale) continue;
    const scaled = count / scale;
    const rounded = scaled < 10 ? Number(scaled.toFixed(1)) : Math.round(scaled);
    return `${rounded}${suffix}`;
  }
  return String(count);
}

export const formatTime = (seconds: number | null | undefined) => seconds == null ? '' : new Date(seconds * 1000).toLocaleString();

export function compact(value: Json | undefined) {
  if (value === undefined) return '';
  if (value !== null && typeof value === 'object') return JSON.stringify(value);
  if (typeof value === 'number' && !Number.isInteger(value)) return String(Number(value.toPrecision(6)));
  return String(value);
}

export function scalars(mapping: Json | undefined, limit = 100) {
  const fields = record(mapping);
  if (!fields) return mapping === undefined || mapping === null ? '' : compact(mapping);
  const short: string[] = [], long: string[] = [];
  for (const [key, value] of Object.entries(fields)) {
    if (typeof value === 'string' && value.length > LONG_TEXT) long.push(`${key} "${value.slice(0, LONG_TEXT).trimEnd()}..."`);
    else short.push(`${key} ${compact(value)}`);
  }
  const text = [...short, ...long].join(', ');
  return text.length <= limit ? text : text.slice(0, limit - 3).trimEnd() + '...';
}

export const visible = (event: EvidenceEvent, perspective: string) =>
  !perspective || event.audience.includes('*') || event.audience.includes(perspective);

export const filterEvents = (events: EvidenceEvent[], perspective: string, kind: string) =>
  events.filter(event => visible(event, perspective) && (!kind || event.kind.includes(kind)));

export function participantOf(event: EvidenceEvent): string | null {
  if (typeof event.payload.participant === 'string') return event.payload.participant;
  const action = record(event.payload.action);
  return typeof action?.participant === 'string' ? action.participant : null;
}

const eventTime = (event: EvidenceEvent) => event.event_time ?? event.ingested;
const emptySlots = (): Slots => ({observation: null, attempted: null, executed: null});

function turnAt(turns: Map<number, Turn>, revision: number, participants: string[]) {
  let turn = turns.get(revision);
  if (!turn) {
    turn = {revision, committed: null, started: null, shared: {}, participants: {}, inherited: null, other: [], seqs: []};
    for (const participant of participants) turn.participants[participant] = emptySlots();
    turns.set(revision, turn);
  }
  return turn;
}

export function buildTimeline(events: EvidenceEvent[], participants: string[]): Turn[] {
  const turns = new Map<number, Turn>();
  const committed = new Set<number>();
  const inheritedEvents = new Map<number, EvidenceEvent[]>();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const {kind, revision, payload} = event;
    if (kind === 'history.inherited' || kind === 'history.inherited.chunk') {
      if (!inheritedEvents.has(revision)) inheritedEvents.set(revision, []);
      inheritedEvents.get(revision)!.push(event);
      const turn = turnAt(turns, revision, participants);
      const parent = typeof payload.environment === 'string' ? payload.environment : null;
      turn.inherited ??= {count: 0, parent, checkpoint: null, first_seq: event.seq, last_seq: event.seq};
      turn.inherited.count += 1; turn.inherited.last_seq = event.seq;
      continue;
    }
    const shared = event.audience.length === 1 && event.audience[0] === '*' && !(kind in SLOT_KINDS) && !OUTCOME_KINDS.has(kind);
    let key = revision;
    if (OUTCOME_KINDS.has(kind)) {
      if (kind === 'transition.committed') committed.add(revision);
      key = revision > 0 ? revision - 1 : revision;
    } else if (shared && (committed.has(revision) || (turns.has(revision - 1) && !turns.has(revision)))) {
      key = revision - 1;
    }
    const turn = turnAt(turns, key, participants);
    turn.seqs.push(event.seq);
    const when = eventTime(event);
    if (when != null && (turn.started === null || when < turn.started)) turn.started = when;
    if (kind === 'transition.committed') {
      turn.committed = {revision, seq: event.seq, state_hash: typeof payload.state_hash === 'string' ? payload.state_hash : null,
        terminated: payload.terminated === true, truncated: payload.truncated === true};
    } else if (kind in SLOT_KINDS) {
      const participant = participantOf(event);
      if (participant === null) {turn.other.push(event); continue;}
      const slots = (turn.participants[participant] ??= emptySlots());
      const slot = SLOT_KINDS[kind];
      if (slots[slot] === null) slots[slot] = event; else turn.other.push(event);
    } else if (shared) {
      for (const [field, value] of Object.entries(payload)) if (value === null || typeof value !== 'object') turn.shared[field] = value;
    } else {
      if (kind === 'session.branched') {
        const branchTurn = turnAt(turns, revision, participants);
        if (branchTurn.inherited) {
          branchTurn.inherited.checkpoint = typeof payload.checkpoint === 'string' ? payload.checkpoint : null;
          branchTurn.inherited.parent ||= typeof payload.parent === 'string' ? payload.parent : null;
        }
      }
      turn.other.push(event);
    }
  }
  for (const [revision, history] of inheritedEvents) {
    const inherited = turns.get(revision)!.inherited!;
    inherited.records = reconstructInherited(history);
    inherited.count = inherited.records.length;
  }
  return [...turns.keys()].sort((a, b) => a - b).map(key => turns.get(key)!);
}

export function inheritedSentence(inherited: Inherited) {
  const checkpoint = inherited.checkpoint ? ` at checkpoint ${shortId(inherited.checkpoint)}` : '';
  const partial = inherited.records?.some(item => !item.complete) ? '; some records need more event pages' : '';
  return `Inherited ${inherited.count} events from parent ${shortId(inherited.parent)}${checkpoint}${partial}.`;
}

export function cellText(event: EvidenceEvent, slot: Slot) {
  const payload = event.payload;
  if (slot === 'observation') return `observed ${scalars(payload.payload)}`.trimEnd();
  if (slot === 'attempted') {
    const action = record(payload.action), receipt = record(payload.receipt);
    let status = typeof receipt?.status === 'string' ? receipt.status : 'submitted';
    if (receipt?.reason) status += `: ${compact(receipt.reason)}`;
    return `attempted ${scalars(action?.payload)} (${status})`;
  }
  const outcome = record(payload.outcome);
  const summary = outcome ? Object.fromEntries(Object.entries(outcome).filter(([key, value]) => key !== 'executed' || value !== true)) : payload.outcome;
  let text = `executed ${scalars(summary)}`.trimEnd();
  if (typeof payload.reward === 'number') text += `, reward ${payload.reward.toFixed(2)}`;
  if (payload.reason) text += ` (${compact(payload.reason)})`;
  return text;
}

export function describe(event: EvidenceEvent) {
  const {kind, payload} = event;
  if (kind === 'session.created') {
    const experiment = record(payload.experiment);
    const roster = Array.isArray(experiment?.participants) ? experiment.participants : [];
    const names = roster.map(entry => record(entry)?.id).filter((id): id is string => typeof id === 'string');
    return names.length ? `Session created with participants ${joinNames(names)}.` : 'Session created.';
  }
  if (kind === 'session.branched') {
    const interventions = record(payload.interventions);
    const suffix = interventions && Object.keys(interventions).length ? ` with interventions ${scalars(interventions)}.` : '.';
    return `Branched from ${shortId(payload.parent)} at checkpoint ${shortId(payload.checkpoint)}${suffix}`;
  }
  if (kind === 'checkpoint.committed') return `Checkpoint ${shortId(payload.id)} saved ${payload.exact_agents ? 'with' : 'without'} exact agent state.`;
  if (kind === 'report') return `Score report revision ${compact(payload.revision)} recorded.`;
  if (kind === 'artifact') return `Artifact ${compact(payload.id)} recorded.`;
  if (kind === 'observation.delivered') return `${participantOf(event)} ${cellText(event, 'observation')}.`;
  if (kind === 'action.attempted') return `${participantOf(event)} ${cellText(event, 'attempted')}.`;
  if (kind === 'action.executed') return `${participantOf(event)} ${cellText(event, 'executed')}.`;
  if (kind === 'transition.committed') return `State revision ${event.revision} committed.`;
  const summary = scalars(payload);
  return summary ? `${kind} recorded: ${summary}.` : `${kind} recorded.`;
}

export function nextRevision(turn: Turn): number | null {
  if (turn.committed) return turn.committed.revision;
  const executed = Object.values(turn.participants).map(slots => slots.executed).find(Boolean);
  return executed ? executed.revision : null;
}

export function turnLabel(turn: Turn) {
  const target = nextRevision(turn);
  return target === null ? `Revision ${turn.revision} (open)` : `Revision ${turn.revision} to ${target}`;
}

export const hasActivity = (turn: Turn) => Object.values(turn.participants).some(slots => SLOTS.some(slot => slots[slot]));

const seriesName = (value: string) => value.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase());

export function summarizeReports(
  events: EvidenceEvent[], participants: string[], reports: ReportEnvelope[] = [],
): EvaluationSummary {
  const ordered = [...reports].sort((a, b) =>
    a.report.evidence_cursor - b.report.evidence_cursor || a.revision - b.revision);
  const metrics = new Map<string, EvaluationMetric>();
  const scorers = new Set<string>();
  const uncertainties = new Set<string>();
  for (const envelope of ordered) {
    const report = envelope.report;
    scorers.add(`${report.scorer}@${report.version}`);
    if (report.uncertainty) uncertainties.add(report.uncertainty);
    for (const [metric, value] of Object.entries(report.metrics)) {
      const id = `metric:${report.scorer}:${report.version}:${metric}`;
      const existing = metrics.get(id);
      const first = existing?.first ?? value;
      const change = existing && typeof first === 'number' && Number.isFinite(first)
        && typeof value === 'number' && Number.isFinite(value) ? value - first : null;
      metrics.set(id, {
        id, label: seriesName(metric), scorer: report.scorer, version: report.version,
        unit: report.metric_definitions?.[metric]?.unit ?? null,
        first, latest: value, change, samples: (existing?.samples ?? 0) + 1,
      });
    }
  }

  const turns = buildTimeline(events, participants).filter(hasActivity);
  const turnByRevision = new Map<number, number>();
  const turnByStartingRevision = new Map<number, number>();
  for (const [index, turn] of turns.entries()) {
    turnByStartingRevision.set(turn.revision, index + 1);
    const revision = turn.committed?.revision ?? nextRevision(turn);
    if (revision !== null) turnByRevision.set(revision, index + 1);
  }
  const eventsBySeq = new Map(events.map(event => [event.seq, event]));
  const reportRevision = new Map<number, number>();
  for (const event of events) {
    if (event.kind === 'report' && typeof event.payload.revision === 'number') {
      reportRevision.set(event.payload.revision, event.revision);
    }
  }
  const latest = ordered.at(-1);
  const latestRevision = latest ? reportRevision.get(latest.revision) : undefined;
  const findings: EvaluationFinding[] = [];
  const seenFindings = new Set<string>();
  for (const envelope of ordered) {
    for (const finding of envelope.report.findings ?? []) {
      const identity = JSON.stringify([
        envelope.report.scorer, envelope.report.version, finding.rule, finding.action_id, finding.outcome_event,
      ]);
      if (seenFindings.has(identity)) continue;
      seenFindings.add(identity);
      const outcome = eventsBySeq.get(finding.outcome_event);
      const turn = !outcome ? null : outcome.kind === 'action.attempted'
        ? turnByStartingRevision.get(outcome.revision) ?? null : turnByRevision.get(outcome.revision) ?? null;
      findings.push({...finding, scorer: envelope.report.scorer, version: envelope.report.version, turn});
    }
  }
  return {
    metrics: [...metrics.values()], findings, scorers: [...scorers],
    latestTurn: latestRevision === undefined ? null : turnByRevision.get(latestRevision) ?? null,
    latestCursor: latest?.report.evidence_cursor ?? null, uncertainties: [...uncertainties],
  };
}

export function buildTurnSeries(
  events: EvidenceEvent[], participants: string[], reports: ReportEnvelope[] = [],
): TurnSeriesProjection {
  const turns = buildTimeline(events, participants).filter(hasActivity);
  const turnByRevision = new Map<number, number>();
  const turnByStartingRevision = new Map<number, number>();
  for (const [index, turn] of turns.entries()) {
    turnByStartingRevision.set(turn.revision, index + 1);
    const revision = turn.committed?.revision ?? nextRevision(turn);
    if (revision !== null) turnByRevision.set(revision, index + 1);
  }
  const result = new Map<string, TurnSeries>();
  const append = (
    id: string, label: string, kind: TurnSeries['kind'], unit: string | null,
    participant: string | null, revision: number, value: number,
  ) => {
    const turn = turnByRevision.get(revision);
    if (turn === undefined) return;
    const series = result.get(id) ?? {id, label, kind, unit, participant, points: []};
    series.points.push({turn, revision, value}); result.set(id, series);
  };
  for (const event of events) {
    if (event.kind === 'action.executed' && typeof event.payload.reward === 'number' && Number.isFinite(event.payload.reward)) {
      const participant = typeof event.payload.participant === 'string' ? event.payload.participant : null;
      if (participant) append(`reward:${participant}`, `${seriesName(participant)} · Reward`, 'reward', 'reward', participant,
        event.revision, event.payload.reward);
    }
    const publicEvent = event.audience.length === 1 && event.audience[0] === '*';
    if (publicEvent && !['transition.committed', 'action.executed'].includes(event.kind)) {
      for (const [field, value] of Object.entries(event.payload)) {
        if (typeof value === 'number' && Number.isFinite(value)) {
          append(`signal:${event.kind}:${field}`, `${event.kind} · ${seriesName(field)}`, 'signal', null, null,
            event.revision, value);
        }
      }
    }
  }
  const eventsBySeq = new Map(events.map(event => [event.seq, event]));
  const activityCounts = turns.map(() => ({observed: 0, attempted: 0, executed: 0, blocked: 0}));
  for (const event of events) {
    const turn = event.kind === 'observation.delivered' || event.kind === 'action.attempted'
      ? turnByStartingRevision.get(event.revision) : event.kind === 'action.executed'
        ? turnByRevision.get(event.revision) : undefined;
    if (turn === undefined) continue;
    const counts = activityCounts[turn - 1];
    if (event.kind === 'observation.delivered') counts.observed += 1;
    else if (event.kind === 'action.attempted') {
      counts.attempted += 1;
      if (record(event.payload.receipt)?.status === 'blocked') counts.blocked += 1;
    } else if (event.kind === 'action.executed') counts.executed += 1;
  }
  for (const [index, turn] of turns.entries()) {
    const revision = turn.committed?.revision ?? nextRevision(turn);
    if (revision === null) continue;
    const counts = activityCounts[index];
    const committed = turn.committed ? eventsBySeq.get(turn.committed.seq) : undefined;
    const activity: [string, string, number][] = [
      ['observed', 'Observations Delivered', counts.observed],
      ['attempted', 'Actions Attempted', counts.attempted],
      ['executed', 'Actions Executed', counts.executed],
      ['blocked', 'Blocked Attempts', counts.blocked],
      ['missing', 'Missing Participants', Array.isArray(committed?.payload.missing) ? committed.payload.missing.length : 0],
    ];
    for (const [id, label, value] of activity) append(`activity:${id}`, label, 'activity', 'count', null, revision, value);
  }
  const reportRevision = new Map<number, number>();
  for (const event of events) {
    if (event.kind === 'report' && typeof event.payload.revision === 'number') {
      reportRevision.set(event.payload.revision, event.revision);
    }
  }
  for (const envelope of reports) {
    const revision = reportRevision.get(envelope.revision);
    if (revision === undefined) continue;
    for (const [metric, value] of Object.entries(envelope.report.metrics)) {
      if (typeof value !== 'number' || !Number.isFinite(value)) continue;
      const definition = envelope.report.metric_definitions?.[metric];
      append(`metric:${envelope.report.scorer}:${envelope.report.version}:${metric}`,
        `${seriesName(metric)} · ${envelope.report.scorer}@${envelope.report.version}`, 'metric', definition?.unit ?? null,
        null, revision, value);
    }
  }
  const findings = new Map<string, Map<number, number>>();
  const seenFindings = new Set<string>();
  for (const envelope of reports) {
    for (const finding of envelope.report.findings ?? []) {
      const identity = JSON.stringify([envelope.report.scorer, envelope.report.version, finding.rule, finding.action_id, finding.outcome_event]);
      if (seenFindings.has(identity)) continue;
      seenFindings.add(identity);
      const outcome = eventsBySeq.get(finding.outcome_event);
      if (!outcome) continue;
      const turn = outcome.kind === 'action.attempted'
        ? turnByStartingRevision.get(outcome.revision) : turnByRevision.get(outcome.revision);
      if (turn === undefined) continue;
      const category = findings.get(finding.category) ?? new Map<number, number>();
      category.set(turn, (category.get(turn) ?? 0) + 1); findings.set(finding.category, category);
    }
  }
  for (const [category, counts] of findings) {
    for (const [turn, value] of counts) {
      const revision = turns[turn - 1].committed?.revision ?? nextRevision(turns[turn - 1]);
      if (revision !== null) append(`finding:${category}`, `Recorded Findings · ${seriesName(category)}`, 'finding', 'count', null, revision, value);
    }
  }
  return {series: [...result.values()]};
}


export function reconstructInherited(events: EvidenceEvent[]): Reconstructed[] {
  const groups = new Map<string, {header: Record<string, Json>; chunks: Map<number, string>; audience: string}>();
  const order: (Reconstructed | string)[] = [];
  for (const event of events) {
    const p = event.payload;
    if (event.kind === 'history.inherited') order.push({complete: true, record: p});
    else if (event.kind === 'history.inherited.chunk') {
      const key = JSON.stringify([event.environment, event.revision, p.environment, p.seq, p.hash]);
      if (!groups.has(key)) {groups.set(key, {header: p, chunks: new Map(), audience: JSON.stringify(event.audience)}); order.push(key);}
      const group = groups.get(key)!;
      if (['parts', 'encoding', 'record_sha256'].some(k => group.header[k] !== p[k]) ||
          group.audience !== JSON.stringify(event.audience) || !Number.isInteger(p.part) || !Number.isInteger(p.parts) ||
          Number(p.part) < 0 || Number(p.part) >= Number(p.parts) || group.chunks.has(Number(p.part)) || typeof p.data !== 'string') {
        throw new Error('Invalid inherited chunk sequence');
      }
      group.chunks.set(Number(p.part), p.data);
    }
  }
  return order.map(item => {
    if (typeof item !== 'string') return item;
    const {header, chunks, audience} = groups.get(item)!;
    if (chunks.size !== header.parts) return {complete: false, environment: String(header.environment), seq: Number(header.seq)};
    if (header.encoding !== 'base64-json-v1') throw new Error('Unsupported inherited encoding');
    const encoded = Array.from({length: Number(header.parts)}, (_, i) => chunks.get(i)!).join('');
    const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
    const original = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes)) as Record<string, Json>;
    if (['environment', 'seq', 'hash'].some(k => original[k] !== header[k]) || JSON.stringify(JSON.parse(String(original.audience))) !== audience) {
      throw new Error('Inherited record identity mismatch');
    }
    // The viewer reconstructs records. Full integrity verification belongs to the supplier SDK.
    return {complete: true, record: original};
  });
}
