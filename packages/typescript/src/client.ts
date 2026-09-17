import type {Action, EvidenceEvent, ExperimentSpec, Json, Observation, Environment, Comparison} from './types.js';
export type * from './types.js';

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
    if (!response.ok) throw new Error(`Environment service returned HTTP ${response.status}`);
    const text = await response.text();
    if (text.length > 16777216) throw new Error('Response size limit exceeded');
    return JSON.parse(text) as T;
  }
  list() {return this.request<Environment[]>('GET', '/v1/environments');}
  get(environment: string) {return this.request<Environment>('GET', `/v1/environments/${encodeURIComponent(environment)}`);}
  create(experiment: ExperimentSpec, operationId: string) {return this.request<Environment>('POST', '/v1/environments', experiment, operationId);}
  observe(environment: string, participant?: string) {return this.request<Observation>('GET', `/v1/environments/${encodeURIComponent(environment)}/observation${participant ? '?participant='+encodeURIComponent(participant) : ''}`);}
  submit(environment: string, action: Action) {return this.request<Record<string, Json>>('POST', `/v1/environments/${encodeURIComponent(environment)}/actions`, action);}
  command<T>(environment: string, operation: string, args: unknown = {}) {return this.request<T>('POST', `/v1/environments/${encodeURIComponent(environment)}/commands`, {operation, arguments:args});}
  events(environment: string, after = 0) {return this.request<{events:EvidenceEvent[];cursor:number}>('GET', `/v1/environments/${encodeURIComponent(environment)}/events?after=${after}`);}
  agentWork(environment: string) {return this.request<{work: Array<{id: string; revision: number; participant: string; generation: number; status: string}>}>('GET', `/v1/environments/${encodeURIComponent(environment)}/agent-work`);}
  cancel(environment: string) {return this.command<{status: 'cancelled'; unresolved_agent_work: string[]; unresolved_operations: string[]}>(environment, 'cancel');}
  reports(environment: string) {return this.request<Json[]>('GET', `/v1/environments/${encodeURIComponent(environment)}/reports`);}
  compare(environments: string[]) {return this.request<Comparison & Json>('POST', '/v1/compare', {environments});}
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
