import type {Action, ActivityPage, ActivitySnapshot, AdvanceResponse, CommandOperation, EvidenceEvent, ExperimentSpec, Json, Observation, Environment, Comparison, ReportEnvelope, SourceAcknowledgement, SourceRecord, SourceRegistration, SourceRegistrationReceipt, SourceStatus, SourceStatusUpdate, TrainingRun, Trajectory, TrajectoryDataset, TrajectoryRecordPage, TrajectorySnapshot, TrajectorySummary, TurnSeriesResponse} from './types.js';
export type * from './types.js';

export class ServiceError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
    readonly requestId: string,
    readonly timestamp: string,
    readonly details: unknown[] | null = null,
  ) {super(message); this.name = 'ServiceError';}
}

async function serviceError(response: Response) {
  const fallback = `Environment service returned HTTP ${response.status}`;
  if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) return new Error(fallback);
  const declaredLength = Number(response.headers.get('content-length'));
  if (Number.isFinite(declaredLength) && declaredLength > 4096) return new Error(fallback);
  try {
    const raw = await response.text();
    if (raw.length > 4096) return new Error(fallback);
    const body: unknown = JSON.parse(raw);
    if (body === null || typeof body !== 'object' || Array.isArray(body)) return new Error(fallback);
    const envelope = body as Record<string, unknown>;
    if (envelope.error === null || typeof envelope.error !== 'object' || Array.isArray(envelope.error)) return new Error(fallback);
    const error = envelope.error as Record<string, unknown>;
    const code = error.code, message = error.message, status = error.status;
    const requestId = error.request_id, timestamp = error.timestamp, details = error.details ?? null;
    if (typeof code !== 'string' || code.length < 1 || code.length > 100 ||
      typeof message !== 'string' || message.length < 1 || message.length > 512 ||
      status !== response.status || typeof requestId !== 'string' || requestId.length < 1 || requestId.length > 128 ||
      typeof timestamp !== 'string' || timestamp.length < 1 || timestamp.length > 100 ||
      (details !== null && (!Array.isArray(details) || details.length > 100))) return new Error(fallback);
    const detail = message.replace(/\s+/g, ' ').trim();
    return new ServiceError(`${fallback}: ${detail}`, code, status, requestId, timestamp, details as unknown[] | null);
  } catch {
    return new Error(fallback);
  }
}

export class EnvironmentClient {
  readonly endpoint: string;
  constructor(endpoint: string, private token: string, allowLoopback = false) {
    const url = new URL(endpoint);
    const local = allowLoopback && url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if ((url.protocol !== 'https:' && !local) || url.username || url.password || url.search || url.hash) throw new Error('HTTPS endpoint required');
    this.endpoint = endpoint.replace(/\/$/, '');
  }
  setToken(token: string) {this.token = token;}
  async request<T>(method: string, path: string, body?: unknown, operationId?: string): Promise<T> {
    const response = await fetch(this.endpoint + path, {method, redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(30000),
      headers: {Authorization: `Bearer ${this.token}`, ...(body === undefined ? {} : {'Content-Type':'application/json'}), ...(operationId ? {'X-Operation-ID':operationId} : {})},
      body: body === undefined ? undefined : JSON.stringify(body)});
    if (!response.ok) throw await serviceError(response);
    const text = await response.text();
    if (text.length > 16777216) throw new Error('Response size limit exceeded');
    return JSON.parse(text) as T;
  }
  async *streamJsonl(path: string): AsyncGenerator<Record<string, Json>> {
    const response = await fetch(this.endpoint + path, {method:'GET', redirect:'error', cache:'no-store', signal:AbortSignal.timeout(30000),
      headers:{Authorization:`Bearer ${this.token}`, Accept:'application/x-ndjson'}});
    if (!response.ok) throw await serviceError(response);
    if (!response.body) throw new Error('Environment service returned no stream');
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffered = '';
    try {
      while (true) {
        const {done, value} = await reader.read();
        buffered += decoder.decode(value, {stream:!done});
        if (buffered.length > 16777216 && !buffered.includes('\n')) throw new Error('Stream record size limit exceeded');
        const lines = buffered.split('\n'); buffered = lines.pop() ?? '';
        for (const line of lines) {
          if (!line.trim()) continue;
          if (line.length > 16777216) throw new Error('Stream record size limit exceeded');
          const parsed: unknown = JSON.parse(line);
          if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Environment service returned malformed JSONL');
          yield parsed as Record<string, Json>;
        }
        if (done) break;
      }
      if (buffered.trim()) {
        const parsed: unknown = JSON.parse(buffered);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Environment service returned malformed JSONL');
        yield parsed as Record<string, Json>;
      }
    } finally {reader.releaseLock();}
  }
  list(options: {limit?: number; cursor?: string} = {}) {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set('limit', String(options.limit));
    if (options.cursor !== undefined) query.set('cursor', options.cursor);
    return this.request<Environment[]>('GET', `/v1/environments${query.size ? '?'+query : ''}`);
  }
  get(environment: string) {return this.request<Environment>('GET', `/v1/environments/${encodeURIComponent(environment)}`);}
  create(experiment: ExperimentSpec, operationId: string) {return this.request<Environment>('POST', '/v1/environments', experiment, operationId);}
  observe(environment: string, participant?: string) {return this.request<Observation>('GET', `/v1/environments/${encodeURIComponent(environment)}/observation${participant ? '?participant='+encodeURIComponent(participant) : ''}`);}
  submit(environment: string, action: Action) {return this.request<Record<string, Json>>('POST', `/v1/environments/${encodeURIComponent(environment)}/actions`, action);}
  command<T>(environment: string, operation: CommandOperation, args: Record<string, Json> = {}) {return this.request<T>('POST', `/v1/environments/${encodeURIComponent(environment)}/commands`, {operation, arguments:args});}
  events(environment: string, after = 0) {return this.request<{events:EvidenceEvent[];cursor:number}>('GET', `/v1/environments/${encodeURIComponent(environment)}/events?after=${after}`);}
  turnSeries(environment: string, options: {startTurn?: number; endTurn?: number; maxPoints?: number} = {}) {
    const query = new URLSearchParams({
      start_turn: String(options.startTurn ?? 1),
      max_points: String(options.maxPoints ?? 300),
    });
    if (options.endTurn !== undefined) query.set('end_turn', String(options.endTurn));
    return this.request<TurnSeriesResponse>('GET', `/v1/environments/${encodeURIComponent(environment)}/turn-series?${query}`);
  }
  activitySnapshot() {return this.request<ActivitySnapshot>('GET', '/v1/activity/snapshot');}
  activity(after = 0) {return this.request<ActivityPage>('GET', `/v1/activity/events?after=${after}`);}
  experimentActivity(experiment: string, after = 0) {return this.request<ActivityPage>('GET', `/v1/experiments/${encodeURIComponent(experiment)}/events?after=${after}`);}
  sessionActivity(environment: string, after = 0) {return this.request<ActivityPage>('GET', `/v1/environments/${encodeURIComponent(environment)}/activity?after=${after}`);}
  async activityStream(after = 0): Promise<ActivityPage> {
    const response = await fetch(this.endpoint + '/v1/activity/events', {redirect:'error', cache:'no-store',
      headers:{Authorization:`Bearer ${this.token}`, Accept:'text/event-stream', 'Last-Event-ID':String(after)}});
    if (!response.ok) throw await serviceError(response);
    const raw = await response.text();
    if (raw.length > 16777216) throw new Error('Response size limit exceeded');
    const events = raw.split(/\n\n+/).flatMap(block => {
      const line = block.split('\n').find(item => item.startsWith('data: '));
      return line ? [JSON.parse(line.slice(6))] : [];
    }) as ActivityPage['events'];
    return {events, cursor:events.length ? events[events.length - 1].id : after};
  }
  agentWork(environment: string) {return this.request<{work: Array<{id: string; revision: number; participant: string; generation: number; status: string}>}>('GET', `/v1/environments/${encodeURIComponent(environment)}/agent-work`);}
  cancel(environment: string) {return this.command<{status: 'cancelled'; unresolved_agent_work: string[]; unresolved_operations: string[]}>(environment, 'cancel');}
  advance(environment: string) {return this.command<AdvanceResponse>(environment, 'advance');}
  credentials(environment: string, participant: string, ttl = 3600) {return this.request<{token: string}>('POST', `/v1/environments/${encodeURIComponent(environment)}/credentials`, {participant, ttl});}
  reports(environment: string) {return this.request<ReportEnvelope[]>('GET', `/v1/environments/${encodeURIComponent(environment)}/reports`);}
  compare(environments: string[]) {return this.request<Comparison & Json>('POST', '/v1/compare', {environments});}
  trajectories(options: {limit?: number; cursor?: string} = {}) {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set('limit', String(options.limit));
    if (options.cursor !== undefined) query.set('cursor', options.cursor);
    return this.request<TrajectorySummary[]>('GET', `/v1/trajectories${query.size ? '?'+query : ''}`);
  }
  trajectory(trajectory: string) {return this.request<Trajectory>('GET', `/v1/trajectories/${encodeURIComponent(trajectory)}`);}
  trajectoryRecords(trajectory: string, options: {after?: number; limit?: number} = {}) {
    const query = new URLSearchParams({after:String(options.after ?? 0), limit:String(options.limit ?? 200)});
    return this.request<TrajectoryRecordPage>('GET', `/v1/trajectories/${encodeURIComponent(trajectory)}/records?${query}`);
  }
  registerTrajectorySource(registration: SourceRegistration | Record<string, Json>) {return this.request<SourceRegistrationReceipt>('POST', '/v1/trajectory-sources', registration);}
  ingestTrajectorySource(source: string, batch: {records: SourceRecord[]} | Record<string, Json>) {return this.request<SourceAcknowledgement>('POST', `/v1/trajectory-sources/${encodeURIComponent(source)}/records`, batch);}
  trajectorySourceStatus(source: string) {return this.request<SourceStatus>('GET', `/v1/trajectory-sources/${encodeURIComponent(source)}/status`);}
  updateTrajectorySource(source: string, status: SourceStatusUpdate | Record<string, Json>) {return this.request<SourceStatusUpdate>('PUT', `/v1/trajectory-sources/${encodeURIComponent(source)}/status`, status);}
  freezeTrajectory(trajectory: string) {return this.request<TrajectorySnapshot>('POST', `/v1/trajectories/${encodeURIComponent(trajectory)}/snapshots`);}
  trajectorySnapshots(trajectory: string, options: {limit?: number} = {}) {
    const query = new URLSearchParams({trajectory, limit: String(options.limit ?? 100)});
    return this.request<TrajectorySnapshot[]>('GET', `/v1/trajectory-snapshots?${query}`);
  }
  trajectorySnapshot(snapshot: string) {return this.request<TrajectorySnapshot>('GET', `/v1/trajectory-snapshots/${encodeURIComponent(snapshot)}`);}
  exportTrajectorySnapshot(snapshot: string) {return this.streamJsonl(`/v1/trajectory-snapshots/${encodeURIComponent(snapshot)}/export`);}
  freezeTrajectoryDataset(name: string, trajectories: string[]) {return this.request<TrajectoryDataset>('POST', '/v1/trajectory-datasets', {name, trajectories});}
  trajectoryDataset(dataset: string) {return this.request<TrajectoryDataset>('GET', `/v1/trajectory-datasets/${encodeURIComponent(dataset)}`);}
  exportTrajectoryDataset(dataset: string) {return this.streamJsonl(`/v1/trajectory-datasets/${encodeURIComponent(dataset)}/export`);}
  trainingRun(trainingRun: string) {return this.request<TrainingRun>('GET', `/v1/training-runs/${encodeURIComponent(trainingRun)}`);}
  trajectoryDatasets(options: {limit?: number} = {}) {return this.request<TrajectoryDataset[]>('GET', `/v1/trajectory-datasets?limit=${options.limit ?? 100}`);}
  trainingRuns(options: {dataset?: string; limit?: number} = {}) {
    const query = new URLSearchParams();
    if (options.dataset !== undefined) query.set('dataset', options.dataset);
    query.set('limit', String(options.limit ?? 100));
    return this.request<TrainingRun[]>('GET', `/v1/training-runs?${query}`);
  }
  async *replay(environment: string): AsyncGenerator<EvidenceEvent> {
    let cursor = 0;
    while (true) {const page = await this.events(environment, cursor); if (!page.events.length) return; yield* page.events; cursor = page.cursor;}
  }
  async *follow(environment: string, signal: AbortSignal): AsyncGenerator<EvidenceEvent> {
    let cursor = 0;
    while (!signal.aborted) {
      const page = await this.events(environment, cursor);
      yield* page.events; cursor = page.cursor;
      if (!page.events.length) await new Promise<void>(resolve => {const timer = setTimeout(done, 1000); function done() {clearTimeout(timer); signal.removeEventListener('abort', done); resolve();} signal.addEventListener('abort', done, {once:true});});
    }
  }
}
