import { EnvironmentClient } from './client.js';
const el = (id) => document.getElementById(id);
let client;
let environment = null;
let events = [];
let cursor = 0;
let generation = 0;
let chosenCheckpoint = '';
const owner = crypto.randomUUID();
const selected = new Set();
const json = (value) => JSON.stringify(value, null, 2);
function message(text) { el('message').textContent = text; }
async function attempt(fn) { try {
    await fn();
}
catch (error) {
    message(error instanceof Error ? error.message : 'Operation failed');
} }
function text(tag, value, className = '') { const node = document.createElement(tag); node.textContent = value; node.className = className; return node; }
async function list() {
    const rows = await client.list();
    const holder = el('environments');
    holder.replaceChildren();
    for (const item of rows) {
        const row = text('div', '', 'environment');
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = selected.has(item.id);
        check.setAttribute('aria-label', 'Compare ' + item.id);
        check.onchange = () => { check.checked ? selected.add(item.id) : selected.delete(item.id); };
        const button = text('button', item.id.slice(0, 12));
        button.append(text('small', `${item.status} · revision ${item.revision}`));
        button.onclick = () => void attempt(() => attach(item.id));
        row.append(check, button);
        holder.append(row);
    }
}
async function attach(id) {
    const ticket = ++generation;
    const item = await client.get(id);
    if (ticket !== generation)
        return;
    environment = item;
    cursor = 0;
    events = [];
    el('environment-title').textContent = item.id.slice(0, 16);
    el('environment-status').textContent = item.status;
    el('lineage').textContent = `Lineage ${item.lineage}${item.parent ? ` · Parent ${item.parent}` : ''}`;
    const stats = el('stats');
    stats.replaceChildren();
    for (const [label, value] of [['Revision', String(item.revision)], ['Participants', String(item.participants.length)], ['Cost', '$' + (item.spent_micros / 1e6).toFixed(6)], ['Reserved', '$' + (item.reserved_micros / 1e6).toFixed(6)]]) {
        const stat = text('div', '', 'stat');
        stat.append(text('span', label), text('strong', value));
        stats.append(stat);
    }
    el('manifest').textContent = json(item.experiment ?? item.environment);
    const perspective = el('perspective');
    perspective.replaceChildren(new Option('Authorized evidence', ''));
    for (const participant of item.participants)
        perspective.add(new Option(participant, participant));
    const caps = item.environment.capabilities;
    el('checkpoint').disabled = !caps?.checkpoint;
    el('resume').disabled = !caps?.resume;
    await loadEvents();
    try {
        const reports = await client.reports(id);
        if (ticket === generation) {
            el('reports').replaceChildren(reports.length ? text('pre', json(reports)) : text('p', 'No scoring revision has been recorded.'));
        }
    }
    catch {
        if (ticket === generation)
            el('reports').textContent = 'Scores require researcher or scorer authority.';
    }
}
async function loadEvents() {
    if (!environment)
        return;
    const id = environment.id;
    const ticket = generation;
    const page = await client.events(id, cursor);
    if (ticket !== generation)
        return;
    cursor = page.cursor;
    events = [...events, ...page.events].slice(-500);
    if (page.events.length || !events.length)
        renderEvents();
    el('timeline-note').textContent = `Showing ${events.length} authorized events through cursor ${cursor}. Export retrieves the full history.`;
}
function renderEvents() {
    const perspective = el('perspective').value;
    const kind = el('kind').value;
    const holder = el('timeline');
    const expanded = new Set(Array.from(holder.querySelectorAll('details[open]')).map(node => node.dataset.eventId));
    holder.replaceChildren();
    for (const event of events.filter(event => (!perspective || event.audience.includes('*') || event.audience.includes(perspective)) && (!kind || event.kind.includes(kind)))) {
        const row = text('article', '', 'event');
        row.append(text('div', '#' + event.seq, 'sequence'));
        const body = text('div', '');
        const details = document.createElement('details');
        details.append(text('summary', event.kind));
        details.dataset.eventId = `${event.environment}:${event.seq}`;
        details.open = expanded.has(details.dataset.eventId);
        details.append(text('p', `Revision ${event.revision} · ${new Date(event.ingested * 1000).toLocaleString()} · ${event.audience.length ? event.audience.join(', ') : 'Researcher evidence'}`, 'meta'));
        details.append(text('pre', json(event.payload)));
        body.append(details);
        if (event.kind === 'checkpoint.committed') {
            const button = text('button', 'Branch from checkpoint', 'quiet');
            button.onclick = () => { chosenCheckpoint = String(event.payload.id); el('checkpoint-label').textContent = chosenCheckpoint; el('branch-dialog').showModal(); };
            body.append(button);
        }
        if (event.kind === 'artifact') {
            const button = text('button', 'Download artifact', 'quiet');
            button.onclick = () => void attempt(async () => { await download(`/v1/environments/${environment.id}/artifacts/${event.payload.id}`, String(event.payload.id)); });
            body.append(button);
        }
        row.append(body);
        holder.append(row);
    }
    if (!holder.children.length)
        holder.append(text('p', 'No recorded events match this perspective.'));
}
async function lease() { if (!environment)
    throw new Error('Select an environment first'); return client.command(environment.id, 'lease', { owner, ttl: 30 }); }
async function download(path, filename) {
    // Fetch through the authenticated client without putting credentials in URLs.
    const response = await fetch(client.endpoint + path, { headers: { Authorization: `Bearer ${el('token').value}` }, redirect: 'error', cache: 'no-store' });
    if (!response.ok)
        throw new Error(`Export returned HTTP ${response.status}`);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(await response.blob());
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
el('connect-form').onsubmit = event => { event.preventDefault(); void attempt(async () => { client = new EnvironmentClient(location.origin, el('token').value, true); await client.request('GET', '/v1/environment'); el('access').hidden = true; el('workspace').hidden = false; el('connection').textContent = 'Connected'; try {
    await list();
}
catch {
    message('Participant credentials can attach by environment ID.');
} }); };
el('attach').onclick = () => void attempt(() => attach(el('environment-id').value.trim()));
el('refresh').onclick = () => void attempt(async () => { await list(); if (environment)
    await attach(environment.id); });
el('load-more').onclick = () => void attempt(loadEvents);
el('perspective').onchange = renderEvents;
el('kind').onchange = renderEvents;
el('checkpoint').onclick = () => void attempt(async () => { const saved = await client.command(environment.id, 'checkpoint', { lease: await lease() }); message('Checkpoint saved: ' + saved.id); await loadEvents(); });
for (const command of ['resume', 'pause', 'cancel'])
    el(command).onclick = () => void attempt(async () => { const activeLease = await lease(); await client.command(environment.id, command === 'resume' ? 'resume' : 'control', { lease: activeLease, ...(command === 'resume' ? {} : { command }) }); await attach(environment.id); message('Session ' + command + ' applied.'); });
el('export').onclick = () => void attempt(() => download(`/v1/environments/${environment.id}/export`, `${environment.id}.jsonl`));
el('compare').onclick = () => void attempt(async () => { if (selected.size < 2)
    throw new Error('Select at least two environments.'); const result = await client.compare([...selected]); el('comparison-panel').hidden = false; renderComparison(result); el('comparison').textContent = json(result); });
el('close-branch').onclick = () => el('branch-dialog').close();
el('branch-form').onsubmit = event => { event.preventDefault(); void attempt(async () => { const interventions = JSON.parse(el('interventions').value); const child = await client.command(environment.id, 'branch', { checkpoint: chosenCheckpoint, interventions, new_environment: crypto.randomUUID().replaceAll('-', '') }); el('branch-dialog').close(); await list(); await attach(child.id); message('Isolated branch created.'); }); };
setInterval(() => { if (environment && document.visibilityState === 'visible')
    void attempt(loadEvents); }, 3000);
function renderComparison(value) {
    const result = value;
    const holder = el('comparison-summary');
    holder.replaceChildren();
    const cards = text('div', '', 'comparison-cards');
    for (const item of result.environments) {
        const card = text('section', '', 'comparison-card');
        card.append(text('h3', item.parent ? 'Branched environment' : 'Parent environment'));
        card.append(text('p', item.environment.slice(0, 12), 'muted'));
        const metrics = item.latest_report?.report.metrics ?? {};
        for (const [name, metric] of Object.entries(metrics)) {
            const row = text('p', '', 'metric');
            row.append(text('span', name.replaceAll('_', ' ')), text('strong', typeof metric === 'object' ? json(metric) : String(metric)));
            card.append(row);
        }
        if (!Object.keys(metrics).length)
            card.append(text('p', 'No scoring report recorded.'));
        cards.append(card);
    }
    holder.append(cards, text('p', result.uncertainty), text('p', result.design, 'muted'));
}
