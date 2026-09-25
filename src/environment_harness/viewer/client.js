export class ServiceError extends Error {
    code;
    status;
    requestId;
    timestamp;
    details;
    constructor(message, code, status, requestId, timestamp, details = null) {
        super(message);
        this.code = code;
        this.status = status;
        this.requestId = requestId;
        this.timestamp = timestamp;
        this.details = details;
        this.name = 'ServiceError';
    }
}
async function serviceError(response) {
    const fallback = `Environment service returned HTTP ${response.status}`;
    if (!response.headers.get('content-type')?.toLowerCase().includes('application/json'))
        return new Error(fallback);
    const declaredLength = Number(response.headers.get('content-length'));
    if (Number.isFinite(declaredLength) && declaredLength > 4096)
        return new Error(fallback);
    try {
        const raw = await response.text();
        if (raw.length > 4096)
            return new Error(fallback);
        const body = JSON.parse(raw);
        if (body === null || typeof body !== 'object' || Array.isArray(body))
            return new Error(fallback);
        const envelope = body;
        if (envelope.error === null || typeof envelope.error !== 'object' || Array.isArray(envelope.error))
            return new Error(fallback);
        const error = envelope.error;
        const code = error.code, message = error.message, status = error.status;
        const requestId = error.request_id, timestamp = error.timestamp, details = error.details ?? null;
        if (typeof code !== 'string' || code.length < 1 || code.length > 100 ||
            typeof message !== 'string' || message.length < 1 || message.length > 512 ||
            status !== response.status || typeof requestId !== 'string' || requestId.length < 1 || requestId.length > 128 ||
            typeof timestamp !== 'string' || timestamp.length < 1 || timestamp.length > 100 ||
            (details !== null && (!Array.isArray(details) || details.length > 100)))
            return new Error(fallback);
        const detail = message.replace(/\s+/g, ' ').trim();
        return new ServiceError(`${fallback}: ${detail}`, code, status, requestId, timestamp, details);
    }
    catch {
        return new Error(fallback);
    }
}
export class EnvironmentClient {
    token;
    endpoint;
    constructor(endpoint, token, allowLoopback = false) {
        this.token = token;
        const url = new URL(endpoint);
        const local = allowLoopback && url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
        if ((url.protocol !== 'https:' && !local) || url.username || url.password || url.search || url.hash)
            throw new Error('HTTPS endpoint required');
        this.endpoint = endpoint.replace(/\/$/, '');
    }
    setToken(token) { this.token = token; }
    async request(method, path, body, operationId) {
        const response = await fetch(this.endpoint + path, { method, redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(30000),
            headers: { Authorization: `Bearer ${this.token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(operationId ? { 'X-Operation-ID': operationId } : {}) },
            body: body === undefined ? undefined : JSON.stringify(body) });
        if (!response.ok)
            throw await serviceError(response);
        const text = await response.text();
        if (text.length > 16777216)
            throw new Error('Response size limit exceeded');
        return JSON.parse(text);
    }
    async *streamJsonl(path) {
        const response = await fetch(this.endpoint + path, { method: 'GET', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(30000),
            headers: { Authorization: `Bearer ${this.token}`, Accept: 'application/x-ndjson' } });
        if (!response.ok)
            throw await serviceError(response);
        if (!response.body)
            throw new Error('Environment service returned no stream');
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffered = '';
        try {
            while (true) {
                const { done, value } = await reader.read();
                buffered += decoder.decode(value, { stream: !done });
                if (buffered.length > 16777216 && !buffered.includes('\n'))
                    throw new Error('Stream record size limit exceeded');
                const lines = buffered.split('\n');
                buffered = lines.pop() ?? '';
                for (const line of lines) {
                    if (!line.trim())
                        continue;
                    if (line.length > 16777216)
                        throw new Error('Stream record size limit exceeded');
                    const parsed = JSON.parse(line);
                    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed))
                        throw new Error('Environment service returned malformed JSONL');
                    yield parsed;
                }
                if (done)
                    break;
            }
            if (buffered.trim()) {
                const parsed = JSON.parse(buffered);
                if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed))
                    throw new Error('Environment service returned malformed JSONL');
                yield parsed;
            }
        }
        finally {
            reader.releaseLock();
        }
    }
    query(values) {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(values))
            if (value !== undefined)
                query.set(key, String(value));
        return query.size ? '?' + query : '';
    }
    key(value) { return encodeURIComponent(value); }
    // -- Service ---------------------------------------------------------------
    capabilities() { return this.request('GET', '/v1/capabilities'); }
    // -- Experiments and sessions ---------------------------------------------
    createExperiment(experiment, operationId) { return this.request('POST', '/v1/experiments', experiment, operationId); }
    experiments(options = {}) { return this.request('GET', `/v1/experiments${this.query(options)}`); }
    experiment(experiment) { return this.request('GET', `/v1/experiments/${this.key(experiment)}`); }
    experimentSessions(experiment, options = {}) { return this.request('GET', `/v1/experiments/${this.key(experiment)}/sessions${this.query(options)}`); }
    scenarioSets(options = {}) { return this.request('GET', `/v1/scenario-sets${this.query(options)}`); }
    scenarioSet(scenarioSet) { return this.request('GET', `/v1/scenario-sets/${this.key(scenarioSet)}`); }
    sessions(options = {}) { return this.request('GET', `/v1/sessions${this.query(options)}`); }
    session(session) { return this.request('GET', `/v1/sessions/${this.key(session)}`); }
    observe(session, participant) { return this.request('GET', `/v1/sessions/${this.key(session)}/participants/${this.key(participant)}/observation`); }
    submit(session, participant, action) { return this.request('POST', `/v1/sessions/${this.key(session)}/participants/${this.key(participant)}/actions`, action); }
    credentials(session, participant, ttl = 3600) { return this.request('POST', `/v1/sessions/${this.key(session)}/participants/${this.key(participant)}/credentials`, { ttl }); }
    command(session, operation, args = {}) { return this.request('POST', `/v1/sessions/${this.key(session)}/commands`, { operation, arguments: args }); }
    invocations(session) { return this.request('GET', `/v1/sessions/${this.key(session)}/invocations`); }
    checkpoints(session, options = {}) { return this.request('GET', `/v1/sessions/${this.key(session)}/checkpoints${this.query(options)}`); }
    createCheckpoint(session, exactAgents = false) { return this.request('POST', `/v1/sessions/${this.key(session)}/checkpoints`, { exact_agents: exactAgents }); }
    checkpoint(session, checkpoint) { return this.request('GET', `/v1/sessions/${this.key(session)}/checkpoints/${this.key(checkpoint)}`); }
    branch(session, request) { return this.request('POST', `/v1/sessions/${this.key(session)}/branches`, request); }
    evidence(session, after = 0) { return this.request('GET', `/v1/sessions/${this.key(session)}/evidence?after=${after}`); }
    scores(session) { return this.request('GET', `/v1/sessions/${this.key(session)}/scores`); }
    turnSeries(session, options = {}) {
        return this.request('GET', `/v1/sessions/${this.key(session)}/turn-series${this.query({ start_turn: options.startTurn ?? 1, max_points: options.maxPoints ?? 300, end_turn: options.endTurn })}`);
    }
    async cancel(session) { return (await this.command(session, 'cancel')).result; }
    async advance(session) { return (await this.command(session, 'advance')).result; }
    // -- Activity --------------------------------------------------------------
    activityHierarchy() { return this.request('GET', '/v1/activity/hierarchy'); }
    activity(after = 0) { return this.request('GET', `/v1/activity?after=${after}`); }
    experimentActivity(experiment, after = 0) { return this.request('GET', `/v1/experiments/${this.key(experiment)}/activity?after=${after}`); }
    sessionActivity(session, after = 0) { return this.request('GET', `/v1/sessions/${this.key(session)}/activity?after=${after}`); }
    async activityStream(after = 0) {
        const response = await fetch(this.endpoint + '/v1/activity', { redirect: 'error', cache: 'no-store',
            headers: { Authorization: `Bearer ${this.token}`, Accept: 'text/event-stream', 'Last-Event-ID': String(after) } });
        if (!response.ok)
            throw await serviceError(response);
        const raw = await response.text();
        if (raw.length > 16777216)
            throw new Error('Response size limit exceeded');
        const events = raw.split(/\n\n+/).flatMap(block => {
            const line = block.split('\n').find(item => item.startsWith('data: '));
            return line ? [JSON.parse(line.slice(6))] : [];
        });
        return { events, cursor: events.length ? events[events.length - 1].id : after };
    }
    // -- Evaluation ------------------------------------------------------------
    compare(sessions) { return this.request('POST', '/v1/comparisons', { sessions }); }
    // -- Policies, trajectories, snapshots, datasets ---------------------------
    policies(options = {}) { return this.request('GET', `/v1/policies${this.query(options)}`); }
    policy(policy) { return this.request('GET', `/v1/policies/${this.key(policy)}`); }
    trajectories(options = {}) { return this.request('GET', `/v1/trajectories${this.query(options)}`); }
    trajectory(trajectory) { return this.request('GET', `/v1/trajectories/${this.key(trajectory)}`); }
    trajectoryRecords(trajectory, options = {}) {
        return this.request('GET', `/v1/trajectories/${this.key(trajectory)}/records${this.query({ after: options.after ?? 0, limit: options.limit ?? 200 })}`);
    }
    trajectoryScores(trajectory) { return this.request('GET', `/v1/trajectories/${this.key(trajectory)}/scores`); }
    freezeTrajectory(trajectory) { return this.request('POST', `/v1/trajectories/${this.key(trajectory)}/snapshots`); }
    trajectorySnapshots(trajectory, options = {}) {
        return this.request('GET', `/v1/trajectories/${this.key(trajectory)}/snapshots${this.query(options)}`);
    }
    snapshots(options = {}) { return this.request('GET', `/v1/snapshots${this.query(options)}`); }
    snapshot(snapshot) { return this.request('GET', `/v1/snapshots/${this.key(snapshot)}`); }
    snapshotRecords(snapshot, options = {}) {
        return this.request('GET', `/v1/snapshots/${this.key(snapshot)}/records${this.query({ after: options.after ?? 0, limit: options.limit ?? 200 })}`);
    }
    /** NDJSON streaming stays a hand-written iterator outside generated decoding. */
    streamSnapshotRecords(snapshot) { return this.streamJsonl(`/v1/snapshots/${this.key(snapshot)}/records`); }
    freezeDataset(name, trajectories) { return this.request('POST', '/v1/datasets', { name, trajectories }); }
    datasets(options = {}) { return this.request('GET', `/v1/datasets${this.query(options)}`); }
    dataset(dataset) { return this.request('GET', `/v1/datasets/${this.key(dataset)}`); }
    datasetRecords(dataset, options = {}) {
        return this.request('GET', `/v1/datasets/${this.key(dataset)}/records${this.query({ after: options.after ?? 0, limit: options.limit ?? 200 })}`);
    }
    streamDatasetRecords(dataset) { return this.streamJsonl(`/v1/datasets/${this.key(dataset)}/records`); }
    trainingRun(trainingRun) { return this.request('GET', `/v1/training-runs/${this.key(trainingRun)}`); }
    trainingRuns(options = {}) { return this.request('GET', `/v1/training-runs${this.query(options)}`); }
    // -- Sources ---------------------------------------------------------------
    registerSource(registration) { return this.request('POST', '/v1/sources', registration); }
    sources(options = {}) { return this.request('GET', `/v1/sources${this.query(options)}`); }
    source(source) { return this.request('GET', `/v1/sources/${this.key(source)}`); }
    ingestSource(source, batch) { return this.request('POST', `/v1/sources/${this.key(source)}/records`, batch); }
    sourceRecords(source, options = {}) {
        return this.request('GET', `/v1/sources/${this.key(source)}/records${this.query({ after: options.after ?? 0, limit: options.limit ?? 200 })}`);
    }
    reportSourceStatus(source, status) { return this.request('POST', `/v1/sources/${this.key(source)}/status-reports`, status); }
    sourceStatus(source) { return this.request('GET', `/v1/sources/${this.key(source)}/status`); }
    async *replay(session) {
        let cursor = 0;
        while (true) {
            const page = await this.evidence(session, cursor);
            if (!page.events.length)
                return;
            yield* page.events;
            cursor = page.cursor;
        }
    }
    async *follow(session, signal) {
        let cursor = 0;
        while (!signal.aborted) {
            const page = await this.evidence(session, cursor);
            yield* page.events;
            cursor = page.cursor;
            if (!page.events.length)
                await new Promise(resolve => { const timer = setTimeout(done, 1000); function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); } signal.addEventListener('abort', done, { once: true }); });
        }
    }
}
