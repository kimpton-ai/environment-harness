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
    list(options = {}) {
        const query = new URLSearchParams();
        if (options.limit !== undefined)
            query.set('limit', String(options.limit));
        if (options.cursor !== undefined)
            query.set('cursor', options.cursor);
        return this.request('GET', `/v1/environments${query.size ? '?' + query : ''}`);
    }
    get(environment) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}`); }
    create(experiment, operationId) { return this.request('POST', '/v1/environments', experiment, operationId); }
    observe(environment, participant) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/observation${participant ? '?participant=' + encodeURIComponent(participant) : ''}`); }
    submit(environment, action) { return this.request('POST', `/v1/environments/${encodeURIComponent(environment)}/actions`, action); }
    command(environment, operation, args = {}) { return this.request('POST', `/v1/environments/${encodeURIComponent(environment)}/commands`, { operation, arguments: args }); }
    events(environment, after = 0) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/events?after=${after}`); }
    turnSeries(environment, options = {}) {
        const query = new URLSearchParams({
            start_turn: String(options.startTurn ?? 1),
            max_points: String(options.maxPoints ?? 300),
        });
        if (options.endTurn !== undefined)
            query.set('end_turn', String(options.endTurn));
        return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/turn-series?${query}`);
    }
    activitySnapshot() { return this.request('GET', '/v1/activity/snapshot'); }
    activity(after = 0) { return this.request('GET', `/v1/activity/events?after=${after}`); }
    experimentActivity(experiment, after = 0) { return this.request('GET', `/v1/experiments/${encodeURIComponent(experiment)}/events?after=${after}`); }
    sessionActivity(environment, after = 0) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/activity?after=${after}`); }
    async activityStream(after = 0) {
        const response = await fetch(this.endpoint + '/v1/activity/events', { redirect: 'error', cache: 'no-store',
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
    agentWork(environment) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/agent-work`); }
    cancel(environment) { return this.command(environment, 'cancel'); }
    advance(environment) { return this.command(environment, 'advance'); }
    credentials(environment, participant, ttl = 3600) { return this.request('POST', `/v1/environments/${encodeURIComponent(environment)}/credentials`, { participant, ttl }); }
    reports(environment) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/reports`); }
    compare(environments) { return this.request('POST', '/v1/compare', { environments }); }
    trajectories(options = {}) {
        const query = new URLSearchParams();
        if (options.limit !== undefined)
            query.set('limit', String(options.limit));
        if (options.cursor !== undefined)
            query.set('cursor', options.cursor);
        return this.request('GET', `/v1/trajectories${query.size ? '?' + query : ''}`);
    }
    trajectory(trajectory) { return this.request('GET', `/v1/trajectories/${encodeURIComponent(trajectory)}`); }
    trajectoryRecords(trajectory, options = {}) {
        const query = new URLSearchParams({ after: String(options.after ?? 0), limit: String(options.limit ?? 200) });
        return this.request('GET', `/v1/trajectories/${encodeURIComponent(trajectory)}/records?${query}`);
    }
    registerTrajectorySource(registration) { return this.request('POST', '/v1/trajectory-sources', registration); }
    ingestTrajectorySource(source, batch) { return this.request('POST', `/v1/trajectory-sources/${encodeURIComponent(source)}/records`, batch); }
    trajectorySourceStatus(source) { return this.request('GET', `/v1/trajectory-sources/${encodeURIComponent(source)}/status`); }
    updateTrajectorySource(source, status) { return this.request('PUT', `/v1/trajectory-sources/${encodeURIComponent(source)}/status`, status); }
    freezeTrajectory(trajectory) { return this.request('POST', `/v1/trajectories/${encodeURIComponent(trajectory)}/snapshots`); }
    trajectorySnapshots(trajectory, options = {}) {
        const query = new URLSearchParams({ trajectory, limit: String(options.limit ?? 100) });
        return this.request('GET', `/v1/trajectory-snapshots?${query}`);
    }
    trajectorySnapshot(snapshot) { return this.request('GET', `/v1/trajectory-snapshots/${encodeURIComponent(snapshot)}`); }
    exportTrajectorySnapshot(snapshot) { return this.streamJsonl(`/v1/trajectory-snapshots/${encodeURIComponent(snapshot)}/export`); }
    freezeTrajectoryDataset(name, trajectories) { return this.request('POST', '/v1/trajectory-datasets', { name, trajectories }); }
    trajectoryDataset(dataset) { return this.request('GET', `/v1/trajectory-datasets/${encodeURIComponent(dataset)}`); }
    exportTrajectoryDataset(dataset) { return this.streamJsonl(`/v1/trajectory-datasets/${encodeURIComponent(dataset)}/export`); }
    trainingRun(trainingRun) { return this.request('GET', `/v1/training-runs/${encodeURIComponent(trainingRun)}`); }
    trajectoryDatasets(options = {}) { return this.request('GET', `/v1/trajectory-datasets?limit=${options.limit ?? 100}`); }
    trainingRuns(options = {}) {
        const query = new URLSearchParams();
        if (options.dataset !== undefined)
            query.set('dataset', options.dataset);
        query.set('limit', String(options.limit ?? 100));
        return this.request('GET', `/v1/training-runs?${query}`);
    }
    async *replay(environment) {
        let cursor = 0;
        while (true) {
            const page = await this.events(environment, cursor);
            if (!page.events.length)
                return;
            yield* page.events;
            cursor = page.cursor;
        }
    }
    async *follow(environment, signal) {
        let cursor = 0;
        while (!signal.aborted) {
            const page = await this.events(environment, cursor);
            yield* page.events;
            cursor = page.cursor;
            if (!page.events.length)
                await new Promise(resolve => { const timer = setTimeout(done, 1000); function done() { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); } signal.addEventListener('abort', done, { once: true }); });
        }
    }
}
