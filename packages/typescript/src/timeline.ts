// Turn grouping and one-line summaries for recorded evidence.
// Mirrors src/environment_harness/presentation.py: same rules, same field names.
// A turn is keyed by the revision an action was taken from. The transition that commits
// the next revision, the executed outcomes and the shared-state broadcast that follows
// carry revision + 1 and are assigned to the previous turn.
import type {EvidenceEvent, Json} from './types.js';

export type Slot = 'observation' | 'attempted' | 'executed';
export const SLOTS: Slot[] = ['observation', 'attempted', 'executed'];
export type Slots = Record<Slot, EvidenceEvent | null>;
export interface Inherited {count: number; parent: string | null; checkpoint: string | null; first_seq: number; last_seq: number;}
export interface Committed {revision: number; seq: number; state_hash: string | null; terminated: boolean; truncated: boolean;}
export interface Turn {
  revision: number; committed: Committed | null; started: number | null; shared: Record<string, Json>;
  participants: Record<string, Slots>; inherited: Inherited | null; other: EvidenceEvent[]; seqs: number[];
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
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const {kind, revision, payload} = event;
    if (kind === 'history.inherited') {
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
  return [...turns.keys()].sort((a, b) => a - b).map(key => turns.get(key)!);
}

export function inheritedSentence(inherited: Inherited) {
  const checkpoint = inherited.checkpoint ? ` at checkpoint ${shortId(inherited.checkpoint)}` : '';
  return `Inherited ${inherited.count} events from parent ${shortId(inherited.parent)}${checkpoint}.`;
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
