import type {Action, ActivityHierarchy, ActivityPage, AdvanceResponse, BranchRequest, CapabilityDocument, CheckpointResource, CollectionPage, CommandOperation, Comparison, EvidenceEvent, Experiment, ExperimentSpec, Json, Observation, Policy, ReportEnvelope, ScenarioSet, Session, SourceAcknowledgement, SourceRecord, SourceRegistration, SourceRegistrationReceipt, SourceStatus, SourceStatusUpdate, TrainingRun, Trajectory, TrajectoryDataset, TrajectoryRecordPage, TrajectorySnapshot, TrajectorySummary, TurnSeriesResponse} from './types.js';
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
  private query(values: Record<string, string | number | undefined>) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) if (value !== undefined) query.set(key, String(value));
    return query.size ? '?' + query : '';
  }
  private key(value: string) {return encodeURIComponent(value);}

  // -- Service ---------------------------------------------------------------
  capabilities() {return this.request<CapabilityDocument>('GET', '/v1/capabilities');}

  // -- Experiments and sessions ---------------------------------------------
  createExperiment(experiment: ExperimentSpec, operationId: string) {return this.request<Record<string, Json>>('POST', '/v1/experiments', experiment, operationId);}
  experiments(options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<Experiment>>('GET', `/v1/experiments${this.query(options)}`);}
  experiment(experiment: string) {return this.request<Experiment>('GET', `/v1/experiments/${this.key(experiment)}`);}
  experimentSessions(experiment: string, options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<Session>>('GET', `/v1/experiments/${this.key(experiment)}/sessions${this.query(options)}`);}
  scenarioSets(options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<ScenarioSet>>('GET', `/v1/scenario-sets${this.query(options)}`);}
  scenarioSet(scenarioSet: string) {return this.request<ScenarioSet>('GET', `/v1/scenario-sets/${this.key(scenarioSet)}`);}
  sessions(options: {experiment?: string; limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<Session>>('GET', `/v1/sessions${this.query(options)}`);}
  session(session: string) {return this.request<Session>('GET', `/v1/sessions/${this.key(session)}`);}
  observe(session: string, participant: string) {return this.request<Observation>('GET', `/v1/sessions/${this.key(session)}/participants/${this.key(participant)}/observation`);}
  submit(session: string, participant: string, action: Action) {return this.request<Record<string, Json>>('POST', `/v1/sessions/${this.key(session)}/participants/${this.key(participant)}/actions`, action);}
  credentials(session: string, participant: string, ttl = 3600) {return this.request<{token: string}>('POST', `/v1/sessions/${this.key(session)}/participants/${this.key(participant)}/credentials`, {ttl});}
  command<T>(session: string, operation: CommandOperation, args: Record<string, Json> = {}) {return this.request<{operation: string; accepted: boolean; result: T}>('POST', `/v1/sessions/${this.key(session)}/commands`, {operation, arguments: args});}
  invocations(session: string) {return this.request<{items: Array<{id: string; revision: number; participant: string; generation: number; status: string}>}>('GET', `/v1/sessions/${this.key(session)}/invocations`);}
  checkpoints(session: string, options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<CheckpointResource>>('GET', `/v1/sessions/${this.key(session)}/checkpoints${this.query(options)}`);}
  createCheckpoint(session: string, exactAgents = false) {return this.request<CheckpointResource>('POST', `/v1/sessions/${this.key(session)}/checkpoints`, {exact_agents: exactAgents});}
  checkpoint(session: string, checkpoint: string) {return this.request<CheckpointResource>('GET', `/v1/sessions/${this.key(session)}/checkpoints/${this.key(checkpoint)}`);}
  branch(session: string, request: BranchRequest) {return this.request<Session>('POST', `/v1/sessions/${this.key(session)}/branches`, request as unknown as Record<string, Json>);}
  evidence(session: string, after = 0) {return this.request<{events: EvidenceEvent[]; cursor: number}>('GET', `/v1/sessions/${this.key(session)}/evidence?after=${after}`);}
  scores(session: string) {return this.request<{items: ReportEnvelope[]}>('GET', `/v1/sessions/${this.key(session)}/scores`);}
  turnSeries(session: string, options: {startTurn?: number; endTurn?: number; maxPoints?: number} = {}) {
    return this.request<TurnSeriesResponse>('GET', `/v1/sessions/${this.key(session)}/turn-series${this.query({start_turn: options.startTurn ?? 1, max_points: options.maxPoints ?? 300, end_turn: options.endTurn})}`);
  }
  async cancel(session: string) {return (await this.command<{status: 'cancelled'; unresolved_agent_work: string[]; unresolved_operations: string[]}>(session, 'cancel')).result;}
  async advance(session: string) {return (await this.command<AdvanceResponse>(session, 'advance')).result;}

  // -- Activity --------------------------------------------------------------
  activityHierarchy() {return this.request<ActivityHierarchy>('GET', '/v1/activity/hierarchy');}
  activity(after = 0) {return this.request<ActivityPage>('GET', `/v1/activity?after=${after}`);}
  experimentActivity(experiment: string, after = 0) {return this.request<ActivityPage>('GET', `/v1/experiments/${this.key(experiment)}/activity?after=${after}`);}
  sessionActivity(session: string, after = 0) {return this.request<ActivityPage>('GET', `/v1/sessions/${this.key(session)}/activity?after=${after}`);}
  async activityStream(after = 0): Promise<ActivityPage> {
    const response = await fetch(this.endpoint + '/v1/activity', {redirect:'error', cache:'no-store',
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

  // -- Evaluation ------------------------------------------------------------
  compare(sessions: string[]) {return this.request<Comparison & Json>('POST', '/v1/comparisons', {sessions});}

  // -- Policies, trajectories, snapshots, datasets ---------------------------
  policies(options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<Policy>>('GET', `/v1/policies${this.query(options)}`);}
  policy(policy: string) {return this.request<Policy>('GET', `/v1/policies/${this.key(policy)}`);}
  trajectories(options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<TrajectorySummary>>('GET', `/v1/trajectories${this.query(options)}`);}
  trajectory(trajectory: string) {return this.request<Trajectory>('GET', `/v1/trajectories/${this.key(trajectory)}`);}
  trajectoryRecords(trajectory: string, options: {after?: number; limit?: number} = {}) {
    return this.request<TrajectoryRecordPage>('GET', `/v1/trajectories/${this.key(trajectory)}/records${this.query({after: options.after ?? 0, limit: options.limit ?? 200})}`);
  }
  trajectoryScores(trajectory: string) {return this.request<{items: ReportEnvelope[]}>('GET', `/v1/trajectories/${this.key(trajectory)}/scores`);}
  freezeTrajectory(trajectory: string) {return this.request<TrajectorySnapshot>('POST', `/v1/trajectories/${this.key(trajectory)}/snapshots`);}
  trajectorySnapshots(trajectory: string, options: {limit?: number; cursor?: string} = {}) {
    return this.request<CollectionPage<TrajectorySnapshot>>('GET', `/v1/trajectories/${this.key(trajectory)}/snapshots${this.query(options)}`);
  }
  snapshots(options: {trajectory?: string; limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<TrajectorySnapshot>>('GET', `/v1/snapshots${this.query(options)}`);}
  snapshot(snapshot: string) {return this.request<TrajectorySnapshot>('GET', `/v1/snapshots/${this.key(snapshot)}`);}
  snapshotRecords(snapshot: string, options: {after?: number; limit?: number} = {}) {
    return this.request<TrajectoryRecordPage>('GET', `/v1/snapshots/${this.key(snapshot)}/records${this.query({after: options.after ?? 0, limit: options.limit ?? 200})}`);
  }
  /** NDJSON streaming stays a hand-written iterator outside generated decoding. */
  streamSnapshotRecords(snapshot: string) {return this.streamJsonl(`/v1/snapshots/${this.key(snapshot)}/records`);}
  freezeDataset(name: string, trajectories: string[]) {return this.request<TrajectoryDataset>('POST', '/v1/datasets', {name, trajectories});}
  datasets(options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<TrajectoryDataset>>('GET', `/v1/datasets${this.query(options)}`);}
  dataset(dataset: string) {return this.request<TrajectoryDataset>('GET', `/v1/datasets/${this.key(dataset)}`);}
  datasetRecords(dataset: string, options: {after?: number; limit?: number} = {}) {
    return this.request<TrajectoryRecordPage>('GET', `/v1/datasets/${this.key(dataset)}/records${this.query({after: options.after ?? 0, limit: options.limit ?? 200})}`);
  }
  streamDatasetRecords(dataset: string) {return this.streamJsonl(`/v1/datasets/${this.key(dataset)}/records`);}
  trainingRun(trainingRun: string) {return this.request<TrainingRun>('GET', `/v1/training-runs/${this.key(trainingRun)}`);}
  trainingRuns(options: {dataset?: string; limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<TrainingRun>>('GET', `/v1/training-runs${this.query(options)}`);}

  // -- Sources ---------------------------------------------------------------
  registerSource(registration: SourceRegistration | Record<string, Json>) {return this.request<SourceRegistrationReceipt>('POST', '/v1/sources', registration);}
  sources(options: {limit?: number; cursor?: string} = {}) {return this.request<CollectionPage<SourceStatus>>('GET', `/v1/sources${this.query(options)}`);}
  source(source: string) {return this.request<SourceStatus>('GET', `/v1/sources/${this.key(source)}`);}
  ingestSource(source: string, batch: {records: SourceRecord[]} | Record<string, Json>) {return this.request<SourceAcknowledgement>('POST', `/v1/sources/${this.key(source)}/records`, batch);}
  sourceRecords(source: string, options: {after?: number; limit?: number} = {}) {
    return this.request<TrajectoryRecordPage>('GET', `/v1/sources/${this.key(source)}/records${this.query({after: options.after ?? 0, limit: options.limit ?? 200})}`);
  }
  reportSourceStatus(source: string, status: SourceStatusUpdate | Record<string, Json>) {return this.request<SourceStatusUpdate>('POST', `/v1/sources/${this.key(source)}/status-reports`, status);}
  sourceStatus(source: string) {return this.request<SourceStatus>('GET', `/v1/sources/${this.key(source)}/status`);}

  async *replay(session: string): AsyncGenerator<EvidenceEvent> {
    let cursor = 0;
    while (true) {const page = await this.evidence(session, cursor); if (!page.events.length) return; yield* page.events; cursor = page.cursor;}
  }
  async *follow(session: string, signal: AbortSignal): AsyncGenerator<EvidenceEvent> {
    let cursor = 0;
    while (!signal.aborted) {
      const page = await this.evidence(session, cursor);
      yield* page.events; cursor = page.cursor;
      if (!page.events.length) await new Promise<void>(resolve => {const timer = setTimeout(done, 1000); function done() {clearTimeout(timer); signal.removeEventListener('abort', done); resolve();} signal.addEventListener('abort', done, {once:true});});
    }
  }
}
