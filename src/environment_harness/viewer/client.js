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
            throw new Error(`Environment service returned HTTP ${response.status}`);
        const text = await response.text();
        if (text.length > 16777216)
            throw new Error('Response size limit exceeded');
        return JSON.parse(text);
    }
    list() { return this.request('GET', '/v1/environments'); }
    get(environment) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}`); }
    create(experiment, operationId) { return this.request('POST', '/v1/environments', experiment, operationId); }
    observe(environment, participant) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/observation${participant ? '?participant=' + encodeURIComponent(participant) : ''}`); }
    submit(environment, action) { return this.request('POST', `/v1/environments/${encodeURIComponent(environment)}/actions`, action); }
    command(environment, operation, args = {}) { return this.request('POST', `/v1/environments/${encodeURIComponent(environment)}/commands`, { operation, arguments: args }); }
    events(environment, after = 0) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/events?after=${after}`); }
    agentWork(environment) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/agent-work`); }
    cancel(environment) { return this.command(environment, 'cancel'); }
    advance(environment) { return this.command(environment, 'advance'); }
    credentials(environment, participant, ttl = 3600) { return this.request('POST', `/v1/environments/${encodeURIComponent(environment)}/credentials`, { participant, ttl }); }
    reports(environment) { return this.request('GET', `/v1/environments/${encodeURIComponent(environment)}/reports`); }
    compare(environments) { return this.request('POST', '/v1/compare', { environments }); }
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
