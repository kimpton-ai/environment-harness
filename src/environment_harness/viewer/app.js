// EnvironmentHarness evidence viewer. It reads recorded evidence and never writes to an
// environment. Checkpoint, resume, cancel and branch are command-line and SDK operations.
import { EnvironmentClient } from './client.js';
import { MISSING, SLOTS, buildTimeline, cellText, describe, filterEvents, formatCost, formatTime, hasActivity, inheritedSentence, shortId, title, turnLabel, scalars, } from './timeline.js';
// State
const el = (id) => document.getElementById(id);
let client;
let catalog = [];
let environment = null;
let events = [];
let cursor = 0;
let generation = 0;
const selected = new Set();
const localViewer = location.protocol === 'http:' && location.hostname === '127.0.0.1';
const localCredentialKey = 'environment-harness-local-credential';
const json = (value) => JSON.stringify(value, null, 2);
// Helpers
function message(text) { el('message').textContent = text; }
async function attempt(fn) { try {
    await fn();
}
catch (error) {
    message(error instanceof Error ? error.message : 'Operation failed');
} }
function text(tag, value, className = '') { const node = document.createElement(tag); node.textContent = value; if (className)
    node.className = className; return node; }
function details(summary, body, className = '', id = '') {
    const node = document.createElement('details');
    node.className = className;
    if (id)
        node.dataset.eventId = id;
    node.append(text('summary', summary), body);
    return node;
}
const known = (id) => catalog.find(row => row.id === id);
function reference(id) { const item = known(id); return item ? `${title(item)} ${shortId(id)}` : shortId(id); }
// API
async function list() {
    catalog = (await client.list()).sort((a, b) => a.lineage.localeCompare(b.lineage) || Number(Boolean(a.parent)) - Number(Boolean(b.parent)) || a.id.localeCompare(b.id));
    renderList();
    return catalog;
}
async function attach(id) {
    const ticket = ++generation;
    const item = await client.get(id);
    if (ticket !== generation)
        return;
    environment = item;
    cursor = 0;
    events = [];
    renderHeader(item);
    const perspective = el('perspective');
    const previous = perspective.value;
    perspective.replaceChildren(new Option('Everyone', ''));
    for (const participant of item.participants)
        perspective.add(new Option(participant, participant));
    if (item.participants.includes(previous))
        perspective.value = previous;
    el('comparison-panel').hidden = true;
    renderList();
    await loadEvents();
    try {
        const reports = await client.reports(id);
        if (ticket === generation)
            renderReports(reports);
    }
    catch {
        if (ticket === generation)
            el('reports').replaceChildren(text('p', 'Scores require researcher or scorer authority.', 'muted'));
    }
}
async function loadEvents() {
    if (!environment)
        return;
    const ticket = generation;
    const page = await client.events(environment.id, cursor);
    if (ticket !== generation)
        return;
    cursor = page.cursor;
    events = [...events, ...page.events].slice(-500);
    if (page.events.length || !events.length)
        renderTimeline();
    el('timeline-note').textContent = events.length === 500
        ? 'The latest 500 authorized events appear here, grouped by revision. Export retrieves the full recorded history.'
        : `${events.length} recorded events, grouped by revision.`;
    el('load-more').hidden = page.events.length < 200;
}
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
async function runCompare(ids) {
    if (ids.length < 2)
        throw new Error('Select at least two environments.');
    const result = await client.compare(ids);
    el('comparison-panel').hidden = false;
    renderComparison(result);
    el('comparison').textContent = json(result);
    el('comparison-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
// Rendering
function renderList() {
    const holder = el('environments');
    holder.replaceChildren();
    for (const item of catalog) {
        const row = text('div', '', 'environment');
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = selected.has(item.id);
        check.setAttribute('aria-label', `Compare ${title(item)}`);
        check.onchange = () => { check.checked ? selected.add(item.id) : selected.delete(item.id); };
        const button = text('button', title(item));
        button.setAttribute('aria-current', String(environment?.id === item.id));
        button.append(text('small', `${item.status}, revision ${item.revision}, ${shortId(item.id)}`));
        button.onclick = () => void attempt(() => attach(item.id));
        row.append(check, button);
        holder.append(row);
    }
    if (!catalog.length)
        holder.append(text('p', 'No environments are recorded in this store yet.', 'muted'));
}
function renderHeader(item) {
    el('environment-title').textContent = title(item);
    el('environment-status').textContent = item.status;
    el('environment-identity').textContent = item.id;
    const stats = el('stats');
    stats.replaceChildren();
    for (const [label, value] of [['Revision', String(item.revision)], ['Participants', String(item.participants.length)], ['Spent', formatCost(item.spent_micros)], ['Reserved', formatCost(item.reserved_micros)]]) {
        const stat = text('div', '', 'stat');
        stat.append(text('strong', value), text('span', label));
        stats.append(stat);
    }
    const spec = item.environment;
    const lineage = item.parent ? `Branched from ${reference(item.parent)}.` : `Original environment in lineage ${shortId(item.lineage)}.`;
    el('lineage').textContent = `${typeof spec.implementation === 'string' ? spec.implementation : String(spec.id)} with ${String(spec.scheduling ?? 'unspecified')} scheduling. ${lineage}`;
    el('manifest').textContent = json(item.experiment ?? item.environment);
}
function renderTimeline() {
    const holder = el('timeline');
    const expanded = new Set(Array.from(holder.querySelectorAll('details[open]')).map(node => node.dataset.eventId));
    holder.replaceChildren();
    const perspective = el('perspective').value;
    const turns = buildTimeline(filterEvents(events, perspective, el('kind').value), environment?.participants ?? []);
    for (const turn of turns)
        holder.append(renderTurn(turn, perspective));
    if (!turns.length)
        holder.append(text('p', 'No recorded events match this perspective.', 'muted'));
    for (const node of holder.querySelectorAll('details[data-event-id]'))
        node.open = expanded.has(node.dataset.eventId);
}
function renderTurn(turn, perspective) {
    const section = text('section', '', 'turn');
    const head = text('div', '', 'turn-head');
    head.append(text('h3', turnLabel(turn)), text('span', formatTime(turn.started), 'time'));
    if (turn.committed?.terminated)
        head.append(text('span', 'terminated', 'tag'));
    else if (turn.committed?.truncated)
        head.append(text('span', 'truncated', 'tag'));
    if (Object.keys(turn.shared).length)
        head.append(text('span', scalars(turn.shared), 'shared'));
    section.append(head);
    const firstSeq = Math.min(...Object.values(turn.participants).flatMap(slots => SLOTS.map(slot => slots[slot]?.seq ?? Infinity)));
    const before = turn.other.filter(event => event.seq < firstSeq), after = turn.other.filter(event => event.seq >= firstSeq);
    if (turn.inherited) {
        section.append(text('p', inheritedSentence(turn.inherited), 'turn-note'));
        for (const item of turn.inherited.records ?? []) {
            section.append(details(item.complete ? 'Inherited record' : 'Incomplete inherited record: load more events', text('pre', json(item)), 'turn-note'));
        }
    }
    for (const event of before)
        section.append(renderNote(event));
    if (hasActivity(turn))
        for (const [name, slots] of Object.entries(turn.participants))
            section.append(renderParticipantRow(name, slots, perspective));
    for (const event of after)
        section.append(renderNote(event));
    return section;
}
function renderParticipantRow(name, slots, perspective) {
    const row = text('div', '', 'turn-row');
    row.append(text('span', name, 'participant'));
    const empty = SLOTS.every(slot => !slots[slot]);
    if (empty) {
        row.append(text('span', perspective && name !== perspective ? 'not visible from this perspective' : 'no events in this turn', 'cell missing'));
        return row;
    }
    for (const slot of SLOTS) {
        const event = slots[slot];
        if (!event) {
            row.append(text('span', MISSING[slot], 'cell missing'));
            continue;
        }
        const cell = details(cellText(event, slot), payload(event), 'cell', `${event.environment}:${event.seq}`);
        if (slot === 'executed' && typeof event.payload.reward === 'number')
            cell.classList.add(event.payload.reward >= 0 ? 'positive' : 'negative');
        row.append(cell);
    }
    return row;
}
function payload(event) {
    const body = text('div', '', 'payload');
    body.append(text('p', `${event.kind}, event ${event.seq}, state revision ${event.revision}, ${formatTime(event.event_time ?? event.ingested)}.`, 'muted'));
    body.append(text('pre', json(event.payload)));
    return body;
}
function renderNote(event) {
    const sentence = describe(event);
    const body = payload(event);
    if (event.kind === 'checkpoint.committed' && environment) {
        body.prepend(text('p', 'To branch from this checkpoint, run the command below.', 'muted'), text('pre', `environment-harness branch ${environment.id} ${String(event.payload.id)}`));
    }
    const note = details(sentence, body, 'turn-note', `${event.environment}:${event.seq}`);
    if (event.kind === 'artifact') {
        const button = text('button', 'Download artifact', 'quiet');
        button.onclick = () => void attempt(() => download(`/v1/environments/${environment.id}/artifacts/${String(event.payload.id)}`, String(event.payload.id)));
        body.append(button);
    }
    return note;
}
function renderReports(value) {
    const holder = el('reports');
    holder.replaceChildren();
    const records = value;
    if (!records.length) {
        holder.append(text('p', 'No score report recorded.', 'muted'));
        return;
    }
    for (const record of records) {
        const block = text('div', '', 'report');
        const findings = record.report.findings?.length ?? 0;
        block.append(text('p', `Score report revision ${record.revision} by ${record.report.scorer}@${record.report.version} at evidence cursor ${record.report.evidence_cursor}.${findings ? ` ${findings} finding${findings === 1 ? '' : 's'}.` : ''}`));
        const metrics = text('div', '', 'metrics');
        for (const [key, metric] of Object.entries(record.report.metrics ?? {})) {
            const row = text('div', '', 'metric');
            row.append(text('strong', typeof metric === 'object' ? JSON.stringify(metric) : String(metric)), text('span', key.replaceAll('_', ' ')));
            metrics.append(row);
        }
        block.append(metrics);
        if (record.report.uncertainty)
            block.append(text('p', record.report.uncertainty, 'muted'));
        block.append(details('Full report record', text('pre', json(record))));
        holder.append(block);
    }
}
function renderComparison(value) {
    const result = value;
    const holder = el('comparison-summary');
    holder.replaceChildren();
    const cards = text('div', '', 'comparison-cards');
    for (const item of result.environments) {
        const card = text('section', '', 'comparison-card');
        const button = text('button', title({ participants: item.participants ?? known(item.environment)?.participants, parent: item.parent, id: item.environment }), 'link');
        button.setAttribute('aria-current', String(environment?.id === item.environment));
        button.onclick = () => void attempt(() => attach(item.environment));
        card.append(button);
        card.append(text('p', `${shortId(item.environment)}, ${item.status ?? 'unknown'}, revision ${item.revision ?? '?'}, cost ${formatCost(item.cost_micros)}.`, 'muted'));
        for (const [key, changed] of Object.entries(item.interventions ?? {}))
            card.append(text('p', `${key.replaceAll('_', ' ')} set to ${typeof changed === 'object' ? JSON.stringify(changed) : String(changed)}.`, 'muted'));
        const metrics = item.latest_report?.report.metrics ?? {};
        for (const [key, metric] of Object.entries(metrics)) {
            const row = text('div', '', 'metric');
            row.append(text('strong', typeof metric === 'object' ? JSON.stringify(metric) : String(metric)), text('span', key.replaceAll('_', ' ')));
            card.append(row);
        }
        if (!Object.keys(metrics).length)
            card.append(text('p', 'No score report recorded.', 'muted'));
        cards.append(card);
    }
    holder.append(cards, text('p', result.uncertainty), text('p', result.design, 'muted'));
    for (const group of result.metric_groups ?? []) {
        const block = text('section', '', 'report');
        block.append(text('p', `${group.metric} by ${group.scorer}@${group.version} (${group.kind}), unit ${group.definition?.unit ?? 'unspecified'}, group ${shortId(group.id)}.`));
        const summary = group.summary;
        block.append(text('p', summary ? `Mean of lineage means ${summary.mean_of_lineage_means}, independent lineages ${summary.independent_lineages}, standard error ${summary.standard_error ?? 'not available'}.` : 'Raw values only. No metric definition was declared.'));
        block.append(text('p', `Reported ${group.reported_environments}/${group.selected_environments}, missing ${group.missing_environments}, incomplete ${group.incomplete_environments}.`, 'muted'));
        block.append(details('Values and selected report revisions', text('pre', json(group))));
        holder.append(block);
    }
    for (const warning of result.warnings ?? [])
        holder.append(text('p', warning, 'muted'));
}
// Connection
async function connect(token, rememberLocal = false) {
    client = new EnvironmentClient(location.origin, token, true);
    await client.request('GET', '/v1/environment');
    el('token').value = token;
    if (localViewer) {
        try {
            sessionStorage.removeItem(localCredentialKey);
            if (rememberLocal)
                sessionStorage.setItem(localCredentialKey, token);
        }
        catch { /* Connection still works when browser storage is disabled. */ }
    }
    el('access').hidden = true;
    el('workspace').hidden = false;
    el('connection').textContent = 'Connected';
    try {
        const rows = await list();
        const initial = rows.find(row => !row.parent) ?? rows[0];
        if (initial)
            await attach(initial.id);
    }
    catch {
        message('Participant credentials can attach by environment ID.');
    }
}
el('connect-form').onsubmit = event => { event.preventDefault(); void attempt(() => connect(el('token').value)); };
async function connectLocal() {
    const params = new URLSearchParams(location.hash.slice(1));
    const ticket = params.get('local-login');
    if (ticket !== null)
        history.replaceState(null, '', location.pathname + location.search);
    if (!localViewer)
        return;
    let token = null;
    try {
        if (ticket !== null) {
            try {
                sessionStorage.removeItem(localCredentialKey);
            }
            catch { /* Storage is optional. */ }
            const response = await fetch('/local/connect', { method: 'POST', headers: { 'X-Local-Login': ticket }, redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(10000) });
            if (!response.ok)
                throw new Error('The local connection link expired. Restart with serve --open to reconnect.');
            token = (await response.json()).token;
        }
        else {
            try {
                token = sessionStorage.getItem(localCredentialKey);
            }
            catch { /* Storage is optional. */ }
        }
        if (token)
            await connect(token, true);
    }
    catch (error) {
        try {
            sessionStorage.removeItem(localCredentialKey);
        }
        catch { /* Storage is optional. */ }
        message(error instanceof Error ? error.message : 'Local connection failed');
    }
}
void connectLocal();
// Wiring
el('attach').onclick = () => void attempt(() => attach(el('environment-id').value.trim()));
el('refresh').onclick = () => void attempt(async () => { await list(); if (environment)
    await attach(environment.id); });
el('load-more').onclick = () => void attempt(loadEvents);
el('perspective').onchange = renderTimeline;
el('kind').onchange = renderTimeline;
el('export').onclick = () => void attempt(() => download(`/v1/environments/${environment.id}/export`, `${environment.id}.jsonl`));
el('compare').onclick = () => void attempt(() => runCompare([...selected]));
el('copy-id').onclick = () => void attempt(async () => { if (environment) {
    await navigator.clipboard.writeText(environment.id);
    message('Environment ID copied.');
} });
setInterval(() => { if (environment && document.visibilityState === 'visible')
    void attempt(loadEvents); }, 3000);
