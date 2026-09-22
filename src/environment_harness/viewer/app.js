// EnvironmentHarness evidence viewer. It reads recorded evidence and never writes to an
// environment. Checkpoint, resume, cancel and branch are command-line and SDK operations.
import { EnvironmentClient } from './client.js';
import { MISSING, SLOTS, buildTimeline, buildTurnSeries, cellText, describe, filterEvents, formatCompactCount, formatCost, formatTime, hasActivity, inheritedSentence, shortId, summarizeReports, title, turnLabel, scalars, } from './timeline.js';
// State
const el = (id) => document.getElementById(id);
let client;
let activeCredential = '';
let catalog = [];
let environment = null;
let events = [];
let reports = [];
let cursor = 0;
let generation = 0;
let sessionSync = 0;
let activitySnapshot = null;
let activityCursor = 0;
let activityTimer = 0;
let activityFailures = 0;
let homePage = 1;
let focusedExperiment = null;
const expandedExperiments = new Set();
let sessionTab = 'overview';
let selectedTurnIndex = -1;
let selectedParticipant = null;
const selected = new Set();
const json = (value) => JSON.stringify(value, null, 2);
// Helpers
function message(value, kind = 'status') {
    if (!value)
        return;
    const region = el('toast-region');
    const toast = text('div', '', `toast toast-${kind}`);
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    toast.setAttribute('aria-atomic', 'true');
    const dismiss = text('button', '×', 'toast-dismiss');
    dismiss.type = 'button';
    dismiss.setAttribute('aria-label', 'Dismiss notification');
    dismiss.title = 'Dismiss notification';
    let timer = 0;
    const remove = () => { window.clearTimeout(timer); toast.remove(); };
    dismiss.onclick = remove;
    toast.append(text('span', value, 'toast-message'), dismiss);
    while (region.childElementCount >= 3)
        region.firstElementChild?.remove();
    region.append(toast);
    timer = window.setTimeout(remove, 6000);
}
async function attempt(fn) { try {
    await fn();
}
catch (error) {
    message(error instanceof Error ? error.message : 'Operation failed', 'error');
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
function bindTurnRangeTrack(track, selection, start, end, onPreview, onCommit) {
    let wheelDelta = 0, wheelTimer = 0, wheelChanged = false;
    selection.onpointerdown = event => {
        const bounds = track.getBoundingClientRect();
        const first = Number(selection.dataset.first), last = Number(selection.dataset.last);
        const maximum = Number(selection.dataset.maximum), span = last - first;
        const pixelsPerTurn = Math.max(4, Math.min(32, bounds.width / Math.max(1, maximum - 1)));
        let changed = false;
        event.preventDefault();
        selection.classList.add('is-dragging');
        selection.setPointerCapture(event.pointerId);
        const origin = event.clientX;
        selection.onpointermove = move => {
            const delta = Math.round((move.clientX - origin) / pixelsPerTurn);
            const nextFirst = Math.max(1, Math.min(maximum - span, first + delta));
            changed ||= nextFirst !== Number(start.value);
            start.value = String(nextFirst);
            end.value = String(nextFirst + span);
            onPreview();
        };
        const finish = (release) => {
            if (selection.hasPointerCapture(release.pointerId))
                selection.releasePointerCapture(release.pointerId);
            selection.classList.remove('is-dragging');
            selection.onpointermove = null;
            selection.onpointerup = null;
            selection.onpointercancel = null;
            if (changed)
                onCommit?.();
        };
        selection.onpointerup = finish;
        selection.onpointercancel = finish;
    };
    track.onwheel = event => {
        const first = Number(selection.dataset.first), last = Number(selection.dataset.last);
        const maximum = Number(selection.dataset.maximum), span = last - first;
        if (maximum <= 1 || span >= maximum - 1)
            return;
        const dominantDelta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
        if (!dominantDelta)
            return;
        event.preventDefault();
        selection.classList.add('is-scrolling');
        wheelDelta += dominantDelta * (event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 33
            : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? 100 : 1);
        window.clearTimeout(wheelTimer);
        wheelTimer = window.setTimeout(() => {
            wheelDelta = 0;
            selection.classList.remove('is-scrolling');
            if (wheelChanged) {
                wheelChanged = false;
                onCommit?.();
            }
        }, 160);
        if (Math.abs(wheelDelta) < 40)
            return;
        const offset = Math.sign(wheelDelta) * Math.max(1, Math.min(3, Math.round(Math.abs(wheelDelta) / 100)));
        wheelDelta = 0;
        const nextFirst = Math.max(1, Math.min(maximum - span, first + offset));
        wheelChanged ||= nextFirst !== first;
        start.value = String(nextFirst);
        end.value = String(nextFirst + span);
        onPreview();
    };
}
function dropdownLabel(select) {
    const explicit = select.getAttribute('aria-label');
    if (explicit)
        return explicit;
    const label = select.closest('label');
    return [...(label?.childNodes ?? [])]
        .filter(node => node.nodeType === Node.TEXT_NODE)
        .map(node => node.textContent?.trim() ?? '').filter(Boolean).join(' ') || 'Options';
}
function enhanceDropdown(select) {
    const existing = select.closest('.viewer-dropdown');
    if (existing)
        return existing;
    const shell = text('div', '', 'viewer-dropdown');
    select.before(shell);
    shell.append(select);
    const menuId = `${select.id}-options`;
    const trigger = text('button', '', 'dropdown-trigger');
    trigger.id = `${select.id}-trigger`;
    trigger.type = 'button';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', menuId);
    trigger.append(text('span', ''));
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 12 12');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm3 4.5 3 3 3-3');
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    svg.append(path);
    trigger.append(svg);
    const menu = text('div', '', 'dropdown-menu');
    menu.id = menuId;
    menu.role = 'listbox';
    menu.setAttribute('aria-label', dropdownLabel(select));
    menu.hidden = true;
    shell.append(trigger, menu);
    return shell;
}
function syncDropdown(select) {
    const shell = select.closest('.viewer-dropdown');
    const trigger = shell?.querySelector('.dropdown-trigger');
    const label = trigger?.querySelector('span');
    const menu = shell?.querySelector('.dropdown-menu');
    if (!shell || !trigger || !label || !menu)
        return;
    label.textContent = select.selectedOptions[0]?.textContent ?? dropdownLabel(select);
    trigger.disabled = select.disabled || select.options.length === 0;
    trigger.setAttribute('aria-label', `${dropdownLabel(select)}: ${label.textContent}`);
    menu.replaceChildren(...[...select.options].map(option => {
        const item = text('button', option.textContent, 'dropdown-option');
        item.type = 'button';
        item.role = 'option';
        item.dataset.value = option.value;
        item.setAttribute('aria-selected', String(option.selected));
        item.tabIndex = option.selected ? 0 : -1;
        item.onclick = () => {
            select.value = option.value;
            select.dispatchEvent(new Event('change', { bubbles: true }));
            menu.hidden = true;
            trigger.setAttribute('aria-expanded', 'false');
            trigger.focus();
        };
        item.onkeydown = event => {
            const options = [...menu.querySelectorAll('.dropdown-option')];
            const index = options.indexOf(item);
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                options[(index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length]?.focus();
            }
            if (event.key === 'Home' || event.key === 'End') {
                event.preventDefault();
                options[event.key === 'Home' ? 0 : options.length - 1]?.focus();
            }
            if (event.key === 'Escape') {
                event.preventDefault();
                menu.hidden = true;
                trigger.setAttribute('aria-expanded', 'false');
                trigger.focus();
            }
        };
        return item;
    }));
}
function initializeDropdown(select) {
    if (select.dataset.dropdownInitialized === 'true')
        return;
    const shell = enhanceDropdown(select);
    const trigger = shell?.querySelector('.dropdown-trigger');
    const menu = shell?.querySelector('.dropdown-menu');
    if (!shell || !trigger || !menu)
        return;
    select.dataset.dropdownInitialized = 'true';
    select.classList.add('dropdown-native');
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;
    const close = () => { menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); };
    const open = () => {
        syncDropdown(select);
        menu.hidden = false;
        trigger.setAttribute('aria-expanded', 'true');
        requestAnimationFrame(() => { if (!menu.hidden)
            menu.querySelector('[aria-selected="true"]')?.focus(); });
    };
    trigger.onclick = () => menu.hidden ? open() : close();
    trigger.onkeydown = event => {
        if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
            event.preventDefault();
            open();
        }
        if (event.key === 'Escape') {
            event.preventDefault();
            close();
        }
    };
    select.addEventListener('change', () => syncDropdown(select));
    document.addEventListener('pointerdown', event => { if (!shell.contains(event.target))
        close(); });
    syncDropdown(select);
}
const participantName = (name) => name.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase());
const sessionStatus = (status) => status === 'succeeded' ? 'Completed' : participantName(status);
const statusFilter = (status) => ['succeeded', 'completed'].includes(status) ? 'completed' : status;
function scenarioTitle(item) {
    const name = item.metadata.name;
    return typeof name === 'string' && name.trim() ? name : item.id;
}
function groupedSessionTitle(id) {
    if (!id || !activitySnapshot)
        return null;
    for (const experiment of activitySnapshot.experiments) {
        for (const scenario of experiment.scenarios) {
            const session = scenario.sessions.find(candidate => candidate.id === id);
            if (session)
                return `${scenarioTitle(scenario)} · Trial ${session.trial}`;
        }
    }
    return null;
}
function sessionTitle(item) {
    const grouped = groupedSessionTitle(item.id);
    if (grouped)
        return grouped;
    const metadata = mapping(item.experiment?.scenario_metadata);
    const name = metadata.name;
    if (typeof name === 'string' && name.trim())
        return name;
    const names = item.participants ?? [];
    if (names.length > 3)
        return `${names.length} Participants`;
    if (names.length) {
        const human = names.map(participantName);
        if (human.length === 1)
            return human[0];
        if (human.length === 2)
            return `${human[0]} and ${human[1]}`;
        return `${human[0]}, ${human[1]}, and ${human[2]}`;
    }
    return title(item);
}
function mapping(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : {};
}
const known = (id) => catalog.find(row => row.id === id);
function reference(id) { const item = known(id); return item ? `${title(item)} ${shortId(id)}` : shortId(id); }
const sessionPath = (id, tab) => `/session/${encodeURIComponent(id)}/${tab}`;
function selectedEnvironments(parameters) {
    return [...new Set(parameters.getAll('environment').filter(Boolean))].slice(0, 100);
}
function positiveTurn(value) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 1 ? parsed : undefined;
}
function selectionPath(path, environments, view = {}) {
    const parameters = new URLSearchParams();
    for (const environment of [...new Set(environments)].slice(0, 100))
        parameters.append('environment', environment);
    if (path === '/compare' && view.metric)
        parameters.set('metric', view.metric);
    if (path === '/compare' && view.startTurn !== undefined)
        parameters.set('start_turn', String(view.startTurn));
    if (path === '/compare' && view.endTurn !== undefined)
        parameters.set('end_turn', String(view.endTurn));
    const query = parameters.toString();
    return path + (query ? `?${query}` : '');
}
function routeFromLocation() {
    const path = location.pathname.replace(/\/$/, '') || '/';
    const parameters = new URLSearchParams(location.search);
    const environments = selectedEnvironments(parameters);
    if (path === '/' || path === '/home')
        return { kind: 'home', environments };
    if (path === '/compare') {
        const metric = parameters.get('metric');
        const startTurn = positiveTurn(parameters.get('start_turn'));
        const endTurn = positiveTurn(parameters.get('end_turn'));
        return { kind: 'compare', environments, view: {
                metric: comparisonMetricDefinitions.some(candidate => candidate.id === metric) ? metric : undefined,
                startTurn,
                endTurn: endTurn !== undefined && (startTurn === undefined || endTurn >= startTurn) ? endTurn : undefined,
            } };
    }
    const experiment = path.match(/^\/experiment\/([^/]+)$/);
    if (experiment) {
        try {
            return { kind: 'experiment', id: decodeURIComponent(experiment[1]) };
        }
        catch {
            return null;
        }
    }
    const match = path.match(/^\/session\/([^/]+)(?:\/(overview|turns|progression|reports))?$/);
    if (!match)
        return null;
    try {
        return { kind: 'session', id: decodeURIComponent(match[1]), tab: (match[2] ?? 'overview') };
    }
    catch {
        return null;
    }
}
function setLocation(path, replace = false) {
    const target = new URL(path, location.origin);
    if (location.pathname === target.pathname && location.search === target.search)
        return;
    history[replace ? 'replaceState' : 'pushState'](null, '', target.pathname + target.search);
}
function restoreSelected(environments) {
    selected.clear();
    for (const id of environments)
        if (known(id))
            selected.add(id);
}
function persistSelection() {
    const route = routeFromLocation();
    if (route?.kind === 'home')
        setLocation(selectionPath('/home', [...selected]), true);
    if (route?.kind === 'compare')
        setLocation(selectionPath('/compare', [...selected], route.view), true);
}
function markListSynced() {
    const now = new Date();
    const node = el('list-synced');
    node.dateTime = now.toISOString();
    node.textContent = `List synced at ${now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
}
// API
async function list() {
    const [listed, snapshot] = await Promise.all([client.list(), client.activitySnapshot()]);
    catalog = listed.sort((a, b) => a.lineage.localeCompare(b.lineage) || Number(Boolean(a.parent)) - Number(Boolean(b.parent)) || a.id.localeCompare(b.id));
    activitySnapshot = snapshot;
    activityCursor = Math.max(activityCursor, snapshot.cursor);
    renderList();
    renderModeSummary();
    markListSynced();
    return catalog;
}
async function refresh() {
    await list();
    await restoreRoute();
}
async function refreshFromButton() {
    const button = el('refresh');
    const feedbackStarted = performance.now();
    button.disabled = true;
    button.classList.add('is-loading');
    button.setAttribute('aria-busy', 'true');
    button.setAttribute('aria-label', 'Refreshing sessions');
    try {
        await refresh();
    }
    catch (error) {
        message(error instanceof Error ? error.message : 'Refresh failed', 'error');
    }
    finally {
        const remainingFeedback = 600 - (performance.now() - feedbackStarted);
        if (remainingFeedback > 0)
            await new Promise(resolve => window.setTimeout(resolve, remainingFeedback));
        button.classList.remove('is-loading');
        button.removeAttribute('aria-busy');
        button.removeAttribute('aria-label');
        button.disabled = false;
    }
}
async function refreshCurrentSession() {
    if (!environment)
        return;
    const id = environment.id;
    const generationTicket = generation;
    const syncTicket = ++sessionSync;
    const turnScroll = document.querySelector('.turn-detail')?.scrollTop ?? 0;
    const item = await client.get(id);
    if (generationTicket !== generation || syncTicket !== sessionSync || environment?.id !== id || routeFromLocation()?.kind !== 'session')
        return;
    environment = item;
    renderHeader(item, sessionTab);
    await loadEvents();
    if (generationTicket !== generation || syncTicket !== sessionSync || environment?.id !== id || routeFromLocation()?.kind !== 'session')
        return;
    try {
        const loadedReports = await client.reports(id);
        if (generationTicket !== generation || syncTicket !== sessionSync || environment?.id !== id || routeFromLocation()?.kind !== 'session')
            return;
        reports = loadedReports;
        renderReports(reports);
        renderReportHistory(reports);
        if (sessionTab === 'progression')
            renderProgression();
    }
    catch { /* authority may omit reports */ }
    requestAnimationFrame(() => { const detail = document.querySelector('.turn-detail'); if (detail)
        detail.scrollTop = turnScroll; });
}
async function refreshSelectedActivity(changes) {
    if (!environment || !changes.some(event => event.environment === environment?.id))
        return;
    await refreshCurrentSession();
}
function scheduleActivity(delay = 1000) {
    clearTimeout(activityTimer);
    activityTimer = window.setTimeout(() => void activityTick(), delay);
}
async function activityTick() {
    try {
        const page = activityFailures >= 3 ? await client.activity(activityCursor) : await client.activityStream(activityCursor);
        const changes = page.events.filter(event => event.id > activityCursor);
        activityCursor = Math.max(activityCursor, page.cursor);
        activityFailures = 0;
        if (changes.length) {
            await list();
            await refreshSelectedActivity(changes);
        }
        scheduleActivity(1000);
    }
    catch {
        activityFailures += 1;
        scheduleActivity(activityFailures >= 3 ? 5000 : Math.min(4000, 500 * 2 ** activityFailures));
    }
}
async function attach(id, tab = 'overview', updateLocation = true) {
    const ticket = ++generation;
    sessionSync += 1;
    const item = await client.get(id);
    if (ticket !== generation)
        return;
    environment = item;
    cursor = 0;
    events = [];
    reports = [];
    const rangeEnd = el('compare-turn-end');
    delete rangeEnd.dataset.initialized;
    rangeEnd.value = '1';
    el('compare-turn-start').value = '1';
    renderHeader(item, tab);
    if (updateLocation)
        setLocation(sessionPath(item.id, tab));
    const perspective = el('perspective');
    const previous = perspective.value;
    perspective.replaceChildren(new Option('Everyone', ''));
    for (const participant of item.participants)
        perspective.add(new Option(participant, participant));
    if (item.participants.includes(previous))
        perspective.value = previous;
    syncDropdown(perspective);
    el('comparison-panel').hidden = true;
    renderList();
    await loadEvents();
    try {
        const loadedReports = await client.reports(id);
        if (ticket === generation) {
            reports = loadedReports;
            renderReports(reports);
            renderReportHistory(reports);
            if (sessionTab === 'progression')
                renderProgression();
        }
    }
    catch {
        if (ticket === generation) {
            const unavailable = () => text('p', 'Scores require researcher or scorer authority.', 'muted');
            el('reports').replaceChildren(unavailable());
            el('report-history').replaceChildren(unavailable());
        }
    }
}
async function loadEvents() {
    if (!environment)
        return;
    const ticket = generation;
    let received = 0;
    while (true) {
        const page = await client.events(environment.id, cursor);
        if (ticket !== generation)
            return;
        cursor = page.cursor;
        events = [...events, ...page.events];
        received += page.events.length;
        if (page.events.length < 200)
            break;
    }
    if (received || !events.length)
        renderTimeline();
    renderModeSummary();
    el('timeline-note').textContent = `${events.length} recorded events, grouped by revision.`;
    el('load-more').hidden = true;
}
async function download(path, filename) {
    // Fetch through the authenticated client without putting credentials in URLs.
    const response = await fetch(client.endpoint + path, { headers: { Authorization: `Bearer ${activeCredential}` }, redirect: 'error', cache: 'no-store' });
    if (!response.ok)
        throw new Error(`Export returned HTTP ${response.status}`);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(await response.blob());
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
async function runCompare(ids, requestedView = {}) {
    if (ids.length < 2)
        throw new Error('Select at least two environments.');
    const loadSeries = (startTurn = 1, endTurn) => Promise.all(ids.map(id => client.turnSeries(id, { startTurn, endTurn, maxPoints: 300 })));
    const [result, fullProjections] = await Promise.all([
        client.compare(ids),
        loadSeries(),
    ]);
    const maximumTurn = Math.max(...fullProjections.map(item => item.total_turns), 1);
    const hasRequestedWindow = requestedView.startTurn !== undefined || requestedView.endTurn !== undefined;
    const startTurn = Math.min(maximumTurn, requestedView.startTurn ?? 1);
    const endTurn = Math.max(startTurn, Math.min(maximumTurn, requestedView.endTurn ?? maximumTurn));
    const projections = hasRequestedWindow && (startTurn !== 1 || endTurn !== maximumTurn)
        ? await loadSeries(startTurn, endTurn) : fullProjections;
    const initialView = hasRequestedWindow ? { ...requestedView, startTurn, endTurn } : requestedView;
    const sessions = (series) => {
        const byEnvironment = new Map(series.map(item => [item.environment, item]));
        return result.environments.map(item => comparisonSessionSeries(item, byEnvironment.get(item.environment)));
    };
    renderComparison(result, sessions(projections), async (startTurn, endTurn) => sessions(await loadSeries(startTurn, endTurn)), initialView, view => setLocation(selectionPath('/compare', ids, view), true));
    if (hasRequestedWindow)
        setLocation(selectionPath('/compare', ids, initialView), true);
    el('comparison').textContent = json(result);
    el('compare-results').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
// Rendering
function homePageSlice(entries) {
    const requested = Number(el('home-page-size').value);
    const pageSize = Number.isInteger(requested) && requested >= 1 && requested <= 50 ? requested : 25;
    const pages = Math.max(1, Math.ceil(entries.length / pageSize));
    homePage = Math.max(1, Math.min(homePage, pages));
    const start = (homePage - 1) * pageSize;
    const end = Math.min(start + pageSize, entries.length);
    el('home-page-summary').textContent = entries.length
        ? `${start + 1}–${end} of ${entries.length} ${entries.length === 1 ? 'entry' : 'entries'}`
        : '0 entries';
    el('home-page-current').textContent = String(homePage);
    el('home-page-total').textContent = String(pages);
    el('home-page-previous').disabled = homePage === 1;
    el('home-page-next').disabled = homePage === pages;
    return entries.slice(start, end);
}
function resetHomePage() {
    homePage = 1;
    const holder = el('home-session-list');
    holder.scrollTop = 0;
    holder.scrollLeft = 0;
    renderList();
}
function renderList() {
    const holder = el('home-session-list');
    const tableScroll = { top: holder.scrollTop, left: holder.scrollLeft };
    const restoreTableScroll = () => requestAnimationFrame(() => {
        holder.scrollTop = tableScroll.top;
        holder.scrollLeft = tableScroll.left;
    });
    holder.replaceChildren();
    const snapshot = activitySnapshot && focusedExperiment ? {
        ...activitySnapshot,
        experiments: activitySnapshot.experiments.filter(item => item.id === focusedExperiment),
        standalone: [],
    } : activitySnapshot;
    const totalSessions = snapshot ? snapshot.standalone.length + snapshot.experiments.reduce((sum, item) => sum + item.sessions.length, 0) : catalog.length;
    el('experiment-count').textContent = String(snapshot?.experiments.length ?? 0);
    el('scenario-count').textContent = String(snapshot?.experiments.reduce((sum, item) => sum + item.scenarios.length, 0) ?? 0);
    el('session-count').textContent = String(totalSessions);
    const activitySummary = el('activity-summary');
    if (snapshot) {
        const parts = [];
        if (snapshot.summary.running)
            parts.push(`${snapshot.summary.running} running`);
        if (snapshot.summary.queued)
            parts.push(`${snapshot.summary.queued} queued`);
        if (snapshot.summary.failed)
            parts.push(`${snapshot.summary.failed} failed`);
        activitySummary.textContent = parts.join(' · ');
        activitySummary.hidden = parts.length === 0;
    }
    else {
        activitySummary.hidden = true;
    }
    const status = el('session-status');
    const currentStatus = status.value;
    const statuses = [...new Set((snapshot
            ? [...snapshot.experiments.map(item => item.status), ...snapshot.experiments.flatMap(item => item.sessions.map(session => session.status)), ...snapshot.standalone.map(item => item.status)]
            : catalog.map(item => item.status)).map(statusFilter))].sort();
    status.replaceChildren(new Option('All statuses', ''), ...statuses.map(value => new Option(sessionStatus(value), value)));
    status.value = statuses.includes(currentStatus) ? currentStatus : '';
    syncDropdown(status);
    const query = el('session-search').value.trim().toLowerCase();
    const rows = catalog.filter(item => {
        const environmentSpec = mapping((item.experiment ?? {}).environment);
        const searchable = [sessionTitle(item), item.id, item.status, item.participants.join(' '),
            String((item.experiment ?? {}).scenario ?? ''), String(environmentSpec.implementation ?? item.environment.implementation ?? '')]
            .join(' ').toLowerCase();
        return (!query || searchable.includes(query)) && (!status.value || statusFilter(item.status) === status.value);
    });
    const sort = el('session-sort').value;
    rows.sort((a, b) => sort === 'oldest' ? a.id.localeCompare(b.id) : b.id.localeCompare(a.id));
    if (snapshot) {
        renderActivityRows(holder, snapshot, query, status.value, sort);
        restoreTableScroll();
        return;
    }
    const visibleCount = el('visible-session-count');
    visibleCount.textContent = `${rows.length} visible`;
    visibleCount.hidden = !query && !status.value;
    const header = text('div', '', 'home-session-row home-session-columns');
    for (const label of ['', 'Name', 'Type', 'Status', 'Sessions', 'Turns']) {
        header.append(text('span', label));
    }
    holder.append(header);
    for (const item of homePageSlice(rows)) {
        const row = text('div', '', 'home-session-row');
        row.dataset.sessionId = item.id;
        row.dataset.turnCount = String(item.revision);
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = selected.has(item.id);
        check.setAttribute('aria-label', `Select ${sessionTitle(item)}`);
        check.onchange = () => {
            check.checked ? selected.add(item.id) : selected.delete(item.id);
            persistSelection();
            renderHomeSelection();
        };
        const session = text('span', '', 'home-session-cell');
        const button = text('button', sessionTitle(item), 'session-name');
        button.onclick = () => void attempt(() => attach(item.id));
        session.append(button, text('small', `${item.parent ? 'Branch' : 'Original'} · ${participantSummary(item.participants)} · ${shortId(item.id)}`));
        row.append(check, session, text('span', 'Session', 'home-session-value'), text('span', sessionStatus(item.status), 'session-status'), text('span', '1', 'home-session-value'), text('span', formatCompactCount(item.revision), 'home-session-value'));
        holder.append(row);
    }
    if (!rows.length)
        holder.append(text('p', catalog.length ? 'No environment sessions match these filters.' : 'No environment sessions found for this credential.', 'home-empty muted'));
    renderHomeSelection();
    renderCompareSessionList();
    restoreTableScroll();
}
function activitySearch(session) {
    return [session.scenario_id, session.id, session.status, session.participants.join(' '), session.latest_activity ?? '',
        session.failure ?? '', session.environment.implementation ?? '', JSON.stringify(session.frozen.scenario_metadata ?? {})]
        .join(' ').toLowerCase();
}
function activityMatches(session, query, status) {
    return (!query || activitySearch(session).includes(query)) && (!status || statusFilter(session.status) === status);
}
function participantSummary(participants) {
    return participants.length ? participants.map(participantName).join(', ') : 'Not recorded';
}
function sessionProgress(session) {
    return session.target_turns === null ? `${formatCompactCount(session.current_turn)} turns`
        : `${formatCompactCount(session.current_turn)} of ${formatCompactCount(session.target_turns)} turns`;
}
const totalRecordedTurns = (sessions) => sessions.reduce((total, session) => total + session.current_turn, 0);
function sessionActivity(session) {
    return session.failure ? `Failed: ${session.failure}` : session.latest_activity ?? 'No activity recorded';
}
function scenarioFor(experiment, session) {
    return experiment.scenarios.find(scenario => scenario.id === session.scenario_id);
}
function experimentSearch(experiment) {
    return [experiment.name, experiment.status, experiment.latest_activity ?? '', Object.keys(experiment.score_summary).join(' ')]
        .join(' ').toLowerCase();
}
function experimentDetail(experiment, matches = null) {
    const score = Object.entries(experiment.score_summary).slice(0, 2)
        .map(([name, value]) => `${name} ${formatCompactCount(value)}`).join(' · ');
    const coverage = matches === null
        ? `${experiment.progress.completed} of ${experiment.progress.total} sessions complete`
        : `${matches} matching of ${experiment.sessions.length} sessions`;
    return [coverage, `${experiment.running} running`, experiment.latest_activity, score].filter(Boolean).join(' · ');
}
function childActivityRow(session, experiment) {
    const row = text('div', '', 'home-session-row scenario-session');
    row.dataset.sessionId = session.id;
    row.dataset.turnCount = String(session.current_turn);
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = selected.has(session.id);
    const scenario = scenarioFor(experiment, session);
    const name = `${scenario ? scenarioTitle(scenario) : session.scenario_id} · Trial ${session.trial}`;
    check.setAttribute('aria-label', `Select ${name}`);
    check.onchange = () => {
        check.checked ? selected.add(session.id) : selected.delete(session.id);
        persistSelection();
        renderList();
    };
    const identity = text('span', '', 'home-session-cell');
    const button = text('button', name, 'session-name');
    button.onclick = () => void attempt(() => attach(session.id));
    identity.append(button, text('small', `${session.scenario_id} · ${participantSummary(session.participants)} · ${sessionProgress(session)} · ${sessionActivity(session)} · Session ${shortId(session.id)}`));
    row.append(check, identity, text('span', 'Session', 'home-session-value'), text('span', sessionStatus(session.status), 'session-status'), text('span', '1', 'home-session-value'), text('span', formatCompactCount(session.current_turn), 'home-session-value'));
    return row;
}
function standaloneActivityRow(session) {
    const item = known(session.id);
    const row = text('div', '', 'home-session-row standalone-session');
    row.dataset.sessionId = session.id;
    row.dataset.turnCount = String(session.current_turn);
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.checked = selected.has(session.id);
    check.setAttribute('aria-label', `Select ${item ? sessionTitle(item) : session.scenario_id}`);
    check.onchange = () => {
        check.checked ? selected.add(session.id) : selected.delete(session.id);
        persistSelection();
        renderHomeSelection();
    };
    const identity = text('span', '', 'home-session-cell');
    const button = text('button', item ? sessionTitle(item) : session.scenario_id, 'session-name');
    button.onclick = () => void attempt(() => attach(session.id));
    identity.append(button, text('small', `Standalone · ${participantSummary(session.participants)} · ${sessionProgress(session)} · ${sessionActivity(session)} · ${shortId(session.id)}`));
    row.append(check, identity, text('span', 'Session', 'home-session-value'), text('span', sessionStatus(session.status), 'session-status'), text('span', '1', 'home-session-value'), text('span', formatCompactCount(session.current_turn), 'home-session-value'));
    return row;
}
function renderActivityRows(holder, snapshot, query, status, sort) {
    const header = text('div', '', 'home-session-row home-session-columns');
    for (const label of ['', 'Name', 'Type', 'Status', 'Sessions', 'Turns'])
        header.append(text('span', label));
    holder.append(header);
    const hasFilter = Boolean(query || status);
    const experiments = snapshot.experiments.map(item => {
        const parent = (!query || experimentSearch(item).includes(query))
            && (!status || statusFilter(item.status) === status);
        const matches = item.sessions.filter(session => activityMatches(session, query, status));
        return { item, parent, matches };
    }).filter(group => group.parent || group.matches.length);
    experiments.sort((a, b) => sort === 'oldest' ? a.item.updated - b.item.updated : b.item.updated - a.item.updated);
    const standalone = snapshot.standalone.filter(session => activityMatches(session, query, status));
    standalone.sort((a, b) => sort === 'oldest' ? a.updated - b.updated : b.updated - a.updated);
    const visible = experiments.reduce((count, group) => count + (hasFilter && !group.parent
        ? group.matches.length : group.item.sessions.length), 0) + standalone.length;
    const page = homePageSlice([
        ...experiments.map(group => ({ kind: 'experiment', id: group.item.id })),
        ...standalone.map(session => ({ kind: 'session', id: session.id })),
    ]);
    const visibleExperiments = new Set(page.filter(entry => entry.kind === 'experiment').map(entry => entry.id));
    const visibleStandalone = new Set(page.filter(entry => entry.kind === 'session').map(entry => entry.id));
    for (const { item, parent, matches } of experiments) {
        if (!visibleExperiments.has(item.id))
            continue;
        const row = text('div', '', 'home-session-row experiment-parent');
        row.dataset.experimentId = item.id;
        const experimentCheck = document.createElement('input');
        experimentCheck.type = 'checkbox';
        const selectedChildren = item.sessions.filter(session => selected.has(session.id)).length;
        experimentCheck.checked = Boolean(item.sessions.length) && selectedChildren === item.sessions.length;
        experimentCheck.indeterminate = selectedChildren > 0 && selectedChildren < item.sessions.length;
        experimentCheck.disabled = item.sessions.length === 0;
        experimentCheck.setAttribute('aria-label', `Select all sessions in ${item.name}`);
        experimentCheck.onclick = event => event.stopPropagation();
        experimentCheck.onchange = () => {
            for (const session of item.sessions)
                experimentCheck.checked ? selected.add(session.id) : selected.delete(session.id);
            if (experimentCheck.checked) {
                expandedExperiments.add(item.id);
            }
            persistSelection();
            renderList();
        };
        const identity = text('span', '', 'home-session-cell experiment-identity');
        const name = text('a', item.name, 'experiment-name');
        name.href = `/experiment/${encodeURIComponent(item.id)}`;
        name.onclick = event => { event.preventDefault(); event.stopPropagation(); showExperiment(item.id); };
        const forcedOpen = Boolean(hasFilter && matches.length);
        const open = expandedExperiments.has(item.id) || forcedOpen;
        const disclosure = text('button', open ? 'Hide sessions' : `View ${item.sessions.length} sessions`, 'experiment-disclosure');
        const childId = `experiment-${item.id}-sessions`;
        disclosure.setAttribute('aria-expanded', String(open));
        disclosure.setAttribute('aria-controls', childId);
        const toggleExperiment = () => { expandedExperiments.has(item.id) ? expandedExperiments.delete(item.id) : expandedExperiments.add(item.id); renderList(); };
        disclosure.onclick = event => { event.stopPropagation(); toggleExperiment(); };
        disclosure.onkeydown = event => {
            if (event.key === 'ArrowRight' && !expandedExperiments.has(item.id)) {
                event.preventDefault();
                expandedExperiments.add(item.id);
                renderList();
            }
            if (event.key === 'ArrowLeft' && expandedExperiments.has(item.id)) {
                event.preventDefault();
                expandedExperiments.delete(item.id);
                renderList();
            }
        };
        const identityCopy = text('span', '', 'experiment-identity-copy');
        identityCopy.append(name, text('small', experimentDetail(item, hasFilter && !parent ? matches.length : null)), disclosure);
        identity.append(identityCopy);
        row.onclick = event => {
            if (event.target.closest('input, button, a'))
                return;
            toggleExperiment();
        };
        row.append(experimentCheck, identity, text('span', 'Experiment', 'home-session-value'), text('span', sessionStatus(item.status), 'session-status'), text('span', String(item.sessions.length), 'home-session-value'), text('span', formatCompactCount(totalRecordedTurns(item.sessions)), 'home-session-value'));
        holder.append(row);
        if (open) {
            const group = text('div', '', 'experiment-children');
            group.id = childId;
            const children = hasFilter && !parent ? matches : item.sessions;
            for (const session of children)
                group.append(childActivityRow(session, item));
            holder.append(group);
        }
    }
    for (const session of standalone)
        if (visibleStandalone.has(session.id))
            holder.append(standaloneActivityRow(session));
    const visibleCount = el('visible-session-count');
    visibleCount.textContent = `${visible} visible`;
    visibleCount.hidden = !query && !status;
    if (!experiments.length && !standalone.length)
        holder.append(text('p', 'No environment sessions match these filters.', 'home-empty muted'));
    renderHomeSelection();
    renderCompareSessionList();
}
function renderHomeSelection() {
    const count = selected.size;
    el('home-selection').hidden = count === 0;
    el('home-selection-count').textContent = `${count} session${count === 1 ? '' : 's'} selected`;
    const button = el('compare');
    button.disabled = count < 2;
    button.textContent = count > 1 ? `Compare ${count} Sessions` : 'Select Another Session';
}
function renderBreadcrumbs(items = []) {
    const home = el('home');
    items.length ? home.removeAttribute('aria-current') : home.setAttribute('aria-current', 'page');
    const trail = el('breadcrumb-trail');
    trail.replaceChildren();
    trail.hidden = items.length === 0;
    items.forEach(item => {
        trail.append(text('span', '/', 'breadcrumb-separator'));
        const current = text('span', item.label, 'breadcrumb-current');
        current.setAttribute('aria-current', 'page');
        trail.append(current);
    });
}
function renderSessionBreadcrumbs(item) {
    renderBreadcrumbs([{ label: sessionTitle(item) }]);
}
function revealHome() {
    sessionSync += 1;
    el('home-view').hidden = false;
    el('session-shell').hidden = true;
    el('compare-sessions-view').hidden = true;
    document.body.classList.add('viewer-home');
    document.body.classList.remove('viewer-session', 'viewer-compare');
}
function showHome(updateLocation = true) {
    focusedExperiment = null;
    revealHome();
    renderBreadcrumbs();
    el('home-eyebrow').textContent = 'Home';
    el('home-title').textContent = 'Environment Sessions';
    el('home-description').textContent = 'Find a run, investigate what changed, or select sessions to compare.';
    el('home-list-title').textContent = 'All Sessions';
    document.title = 'Environment Sessions · EnvironmentHarness';
    if (updateLocation)
        setLocation(selectionPath('/home', [...selected]));
    renderList();
}
function showExperiment(id, updateLocation = true) {
    const item = activitySnapshot?.experiments.find(experiment => experiment.id === id);
    focusedExperiment = id;
    expandedExperiments.add(id);
    revealHome();
    renderBreadcrumbs([{ label: item?.name ?? 'Experiment' }]);
    el('home-eyebrow').textContent = 'Experiment';
    el('home-title').textContent = item?.name ?? 'Experiment';
    el('home-description').textContent = item ? experimentDetail(item) : 'Experiment activity is unavailable.';
    el('home-list-title').textContent = 'Experiment Sessions';
    document.title = `${item?.name ?? 'Experiment'} · EnvironmentHarness`;
    if (updateLocation)
        setLocation(`/experiment/${encodeURIComponent(id)}`);
    renderList();
}
function showEmptyState() {
    showHome();
}
function showSession() {
    el('home-view').hidden = true;
    el('session-shell').hidden = false;
    el('compare-sessions-view').hidden = true;
    document.body.classList.remove('viewer-home', 'viewer-compare');
    document.body.classList.add('viewer-session');
    el('empty-state').hidden = true;
    el('session-view').hidden = false;
    renderSessionTab();
}
function setComparisonPickerVisible(visible) {
    el('compare-session-list').hidden = !visible;
    el('run-comparison').hidden = !visible;
}
function showCompareSessions(updateLocation = true, showPicker = selected.size < 2) {
    sessionSync += 1;
    el('home-view').hidden = true;
    el('session-shell').hidden = true;
    el('compare-sessions-view').hidden = false;
    setComparisonPickerVisible(showPicker);
    renderBreadcrumbs([{ label: 'Compare Sessions' }]);
    document.body.classList.remove('viewer-home', 'viewer-session');
    document.body.classList.add('viewer-compare');
    document.title = 'Compare Sessions · EnvironmentHarness';
    if (updateLocation)
        setLocation(selectionPath('/compare', [...selected]));
    renderCompareSessionList();
}
function renderHeader(item, tab = 'overview') {
    sessionTab = tab;
    showSession();
    renderSessionBreadcrumbs(item);
    el('context-title').textContent = sessionTitle(item);
    el('environment-title').textContent = sessionTitle(item);
    el('environment-status').textContent = item.status;
    el('environment-identity').textContent = item.id;
    document.title = `${sessionTitle(item)} · EnvironmentHarness`;
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
    renderFrozenSummary(item);
    renderModeSummary();
}
function renderFrozenSummary(item) {
    const holder = el('frozen-summary');
    holder.replaceChildren();
    const experiment = item.experiment ?? {};
    const policy = mapping(experiment.policy);
    const environmentSpec = mapping(experiment.environment);
    const scoring = Array.isArray(experiment.scoring_versions)
        ? experiment.scoring_versions.map(String).join(', ') : 'Not declared';
    const operations = Array.isArray(experiment.operations)
        ? experiment.operations.map(value => mapping(value).name).filter(Boolean).map(String).join(', ')
        : '';
    const groups = [
        ['Experiment', [
                ['Purpose', String(experiment.purpose ?? 'Unspecified')],
                ['Scenario', String(experiment.scenario ?? 'Unspecified')],
                ['Split', String(experiment.split ?? 'Unspecified')],
                ['Seed', String(experiment.seed ?? 'Unspecified')],
            ]],
        ['Environment', [
                ['Implementation', String(environmentSpec.implementation ?? item.environment.implementation ?? 'Unspecified')],
                ['Scheduling', String(environmentSpec.scheduling ?? item.environment.scheduling ?? 'Unspecified')],
                ['Time Boundary', String(experiment.time_boundary ?? 'Unspecified')],
                ['Participants', `${item.participants.length} Participants`],
                ['Operations', operations || 'None'],
            ]],
        ['Policy', [
                ['Max Turns', String(policy.max_turns ?? 'Unspecified')],
                ['External Writes', policy.external_writes === true ? 'Allowed' : 'Not Allowed'],
                ['Scoring', scoring],
                ['Participant IDs', item.participants.map(participantName).join(', ')],
            ]],
    ];
    for (const [heading, fields] of groups) {
        const section = text('section', '', 'frozen-group');
        section.append(text('h3', heading));
        const list = document.createElement('dl');
        for (const [label, value] of fields) {
            const row = text('div', '');
            row.append(text('dt', label), text('dd', value));
            list.append(row);
        }
        section.append(list);
        holder.append(section);
    }
}
function renderModeSummary() {
    const holder = el('mode-summary');
    holder.replaceChildren();
    const rows = [
        ['Participants', String(environment?.participants.length ?? 0)],
        ['Actions', String(events.filter(event => event.kind === 'action.executed').length)],
        ['Turns', String(environment?.revision ?? 0)],
    ];
    for (const [label, value] of rows) {
        const stat = text('div', '', 'summary-stat');
        stat.append(text('span', label, 'summary-label'), text('span', value, 'summary-value'));
        holder.append(stat);
    }
    el('mode-description').textContent = sessionTab === 'overview'
        ? 'Review this session and its frozen configuration.'
        : sessionTab === 'turns'
            ? 'Inspect this session turn by turn.'
            : sessionTab === 'progression'
                ? 'Follow recorded signals as this session evolves.'
                : 'Inspect versioned evaluation records for this session.';
}
function renderSessionTab() {
    for (const tab of document.querySelectorAll('[data-session-tab]')) {
        tab.setAttribute('aria-selected', String(tab.dataset.sessionTab === sessionTab));
    }
    el('session-overview').hidden = sessionTab !== 'overview';
    el('inspect-view').hidden = sessionTab !== 'turns';
    el('progression-view').hidden = sessionTab !== 'progression';
    el('reports-view').hidden = sessionTab !== 'reports';
    if (sessionTab === 'progression')
        renderProgression();
    renderModeSummary();
}
async function setSessionTab(next, updateLocation = true) {
    sessionTab = next;
    renderSessionTab();
    if (updateLocation && environment)
        setLocation(sessionPath(environment.id, next));
    await refreshCurrentSession();
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
    renderInspect();
}
function inspectTurns() {
    return buildTimeline(events, environment?.participants ?? []).filter(hasActivity);
}
function slotValue(turn, participant, slot) {
    const event = turn.participants[participant]?.[slot];
    return event ? cellText(event, slot) : MISSING[slot];
}
function renderInspect() {
    const turns = inspectTurns();
    if (selectedTurnIndex < 0 || selectedTurnIndex >= turns.length)
        selectedTurnIndex = Math.max(0, turns.length - 1);
    const list = el('turn-list');
    list.replaceChildren();
    el('turn-count').textContent = String(turns.length);
    turns.forEach((turn, index) => {
        const button = text('button', '', 'turn-select');
        button.dataset.turn = String(index + 1);
        button.setAttribute('aria-current', String(index === selectedTurnIndex));
        button.append(text('span', `Turn ${index + 1}`), text('small', turnLabel(turn)));
        button.onclick = () => { selectedTurnIndex = index; selectedParticipant = null; renderInspect(); };
        list.append(button);
    });
    if (!turns.length) {
        list.append(text('p', 'No recorded turns.', 'muted'));
        el('participant-evidence-list').replaceChildren();
        return;
    }
    const turn = turns[selectedTurnIndex];
    const active = Object.entries(turn.participants).filter(([, slots]) => SLOTS.some(slot => slots[slot]));
    selectedParticipant = active.some(([name]) => name === selectedParticipant)
        ? selectedParticipant : active[0]?.[0] ?? null;
    el('turn-detail-title').textContent = `Turn ${selectedTurnIndex + 1}`;
    const previous = el('previous-turn'), next = el('next-turn');
    previous.disabled = selectedTurnIndex === 0;
    next.disabled = selectedTurnIndex === turns.length - 1;
    previous.onclick = () => {
        if (selectedTurnIndex > 0) {
            selectedTurnIndex -= 1;
            selectedParticipant = null;
            renderInspect();
        }
    };
    next.onclick = () => {
        if (selectedTurnIndex < turns.length - 1) {
            selectedTurnIndex += 1;
            selectedParticipant = null;
            renderInspect();
        }
    };
    const progression = el('turn-progression');
    progression.replaceChildren();
    const executed = active.filter(([, slots]) => slots.executed).length;
    const observed = active.filter(([, slots]) => slots.observation).length;
    for (const [label, value] of [
        ['Starting State', `Revision ${turn.revision}`],
        ['Participant Evidence', `${observed} Observations · ${executed} Actions`],
        ['Environment Resolution', `${executed} Actions Applied`],
        ['Committed State', turn.committed ? `Revision ${turn.committed.revision}` : 'Open'],
    ]) {
        const step = text('div', label, 'progression-step');
        step.append(text('strong', value));
        progression.append(step);
    }
    el('starting-state-value').textContent = `Revision ${turn.revision}${Object.keys(turn.shared).length ? ` · ${scalars(turn.shared)}` : ''}`;
    const participants = el('participant-evidence-list');
    participants.replaceChildren();
    for (const [name] of active) {
        const row = text('button', '', 'participant-evidence-row');
        row.dataset.participant = name;
        row.setAttribute('aria-pressed', String(name === selectedParticipant));
        row.append(text('span', participantName(name), 'participant-name'));
        for (const [label, slot] of [
            ['Observation', 'observation'], ['Action', 'attempted'], ['Outcome', 'executed'],
        ]) {
            const piece = text('span', '', 'evidence-piece');
            piece.append(text('span', label, 'evidence-label'), text('span', slotValue(turn, name, slot), 'evidence-value'));
            row.append(piece);
        }
        row.onclick = () => { selectedParticipant = name; renderInspect(); };
        participants.append(row);
    }
    const resolution = el('environment-resolution');
    resolution.replaceChildren(text('h3', 'Environment Resolution'));
    resolution.append(text('p', turn.committed
        ? `${executed} recorded action${executed === 1 ? '' : 's'} produced committed revision ${turn.committed.revision}.`
        : 'This turn has not recorded a committed state.'));
    renderSelectedEvidence(turn);
}
function renderSelectedEvidence(turn) {
    const holder = el('selected-evidence');
    holder.replaceChildren();
    if (!selectedParticipant) {
        holder.append(text('p', 'No participant evidence was recorded for this turn.'));
        return;
    }
    const heading = text('div', '', 'selected-evidence-heading');
    heading.append(text('h3', participantName(selectedParticipant)), text('span', `Turn ${selectedTurnIndex + 1} · Revision ${turn.revision}`, 'muted'));
    holder.append(heading);
    const grid = text('div', '', 'evidence-summary-grid');
    const labels = { observation: 'Observation', attempted: 'Action', executed: 'Outcome' };
    for (const slot of SLOTS) {
        const event = turn.participants[selectedParticipant]?.[slot];
        if (!event)
            continue;
        const summary = text('section', '', 'evidence-summary');
        summary.append(text('span', labels[slot], 'evidence-summary-label'));
        summary.append(text('p', cellText(event, slot), 'evidence-summary-copy'));
        summary.append(text('p', `${event.kind} · Event ${event.seq}`, 'evidence-summary-meta'));
        grid.append(summary);
    }
    holder.append(grid);
}
function renderProgression() {
    const turns = inspectTurns();
    const projection = buildTurnSeries(events, environment?.participants ?? [], reports);
    const select = el('turn-series-select');
    const previousSeries = select.value;
    const rewardSeries = projection.series.filter(series => series.kind === 'reward');
    const metricSeries = projection.series.filter(series => series.kind === 'metric');
    const signalSeries = projection.series.filter(series => series.kind === 'signal');
    const activitySeries = projection.series.filter(series => series.kind === 'activity');
    const findingSeries = projection.series.filter(series => series.kind === 'finding');
    select.replaceChildren();
    if (rewardSeries.length)
        select.add(new Option('Participant Reward', 'reward'));
    for (const series of metricSeries)
        select.add(new Option(series.label, series.id));
    for (const series of signalSeries)
        select.add(new Option(series.label, series.id));
    for (const series of activitySeries)
        select.add(new Option(series.label, series.id));
    for (const series of findingSeries)
        select.add(new Option(series.label, series.id));
    const available = new Set(Array.from(select.options).map(option => option.value));
    const metricDefault = [...metricSeries].sort((a, b) => new Set(b.points.map(point => point.value)).size - new Set(a.points.map(point => point.value)).size
        || b.points.length - a.points.length || a.label.localeCompare(b.label))[0];
    select.value = available.has(previousSeries) ? previousSeries
        : metricDefault?.id ?? signalSeries[0]?.id ?? (rewardSeries.length ? 'reward' : '');
    const participant = el('turn-series-participant');
    const previousParticipant = participant.value;
    participant.replaceChildren(new Option('All Participants', ''));
    for (const name of environment?.participants ?? [])
        participant.add(new Option(participantName(name), name));
    participant.value = (environment?.participants ?? []).includes(previousParticipant) ? previousParticipant : '';
    syncDropdown(select);
    syncDropdown(participant);
    el('turn-series-participant-label').hidden = select.value !== 'reward';
    const selectedSeries = select.value === 'reward'
        ? rewardSeries.filter(series => !participant.value || series.participant === participant.value)
        : projection.series.filter(series => series.id === select.value);
    const start = el('compare-turn-start');
    const end = el('compare-turn-end');
    const maximum = Math.max(1, turns.length);
    start.max = String(maximum);
    end.max = String(maximum);
    if (!end.dataset.initialized && turns.length) {
        end.value = String(maximum);
        end.dataset.initialized = 'true';
    }
    if (Number(start.value) < 1 || Number(start.value) > maximum)
        start.value = '1';
    if (Number(end.value) < 1 || Number(end.value) > maximum)
        end.value = String(maximum);
    let first = Math.min(Number(start.value), Number(end.value));
    let last = Math.max(Number(start.value), Number(end.value));
    if (!turns.length) {
        first = 0;
        last = 0;
    }
    const selection = el('turn-range-selection');
    const denominator = Math.max(1, maximum - 1);
    selection.style.left = `${turns.length ? ((first - 1) / denominator) * 100 : 0}%`;
    selection.style.width = `${turns.length ? ((last - first) / denominator) * 100 : 0}%`;
    selection.dataset.first = String(first);
    selection.dataset.last = String(last);
    selection.dataset.maximum = String(maximum);
    const visible = turns.slice(Math.max(0, first - 1), last);
    el('turn-compare-summary').textContent = `${visible.length} turn${visible.length === 1 ? '' : 's'} in view`;
    el('turn-range-start-value').textContent = turns.length ? `Turn ${first}` : 'No turn';
    el('turn-range-end-value').textContent = turns.length ? `Turn ${last}` : 'No turn';
    const chart = document.getElementById('turn-series-chart');
    chart.replaceChildren();
    const chartWidth = Math.max(840, Math.round(chart.getBoundingClientRect().width) || 1120);
    chart.setAttribute('viewBox', `0 0 ${chartWidth} 280`);
    const svg = (tag, attributes, value = '') => {
        const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
        for (const [key, attribute] of Object.entries(attributes))
            node.setAttribute(key, attribute);
        if (value)
            node.textContent = value;
        return node;
    };
    const left = 58, right = chartWidth - 22, top = 20, bottom = 226;
    const visibleSeries = selectedSeries.map(series => ({ ...series, points: series.points.filter(point => point.turn >= first && point.turn <= last) }));
    const values = visibleSeries.flatMap(series => series.points.map(point => point.value));
    let minimum = values.length ? Math.min(...values) : 0;
    let maximumValue = values.length ? Math.max(...values) : 1;
    if (minimum === maximumValue) {
        minimum -= Math.abs(minimum || 1) * .1;
        maximumValue += Math.abs(maximumValue || 1) * .1;
    }
    const x = (turn) => left + ((turn - first) / Math.max(1, last - first)) * (right - left);
    const y = (value) => bottom - ((value - minimum) / Math.max(Number.EPSILON, maximumValue - minimum)) * (bottom - top);
    const formatValue = (value) => Number(value.toPrecision(6)).toString();
    for (const ratio of [0, .5, 1]) {
        const lineY = bottom - ratio * (bottom - top);
        chart.append(svg('line', { x1: String(left), x2: String(right), y1: String(lineY), y2: String(lineY), class: 'series-grid' }));
        chart.append(svg('text', { x: String(left - 10), y: String(lineY + 4), class: 'series-axis-label', 'text-anchor': 'end' }, formatValue(minimum + ratio * (maximumValue - minimum))));
    }
    for (const turn of [...new Set([first, Math.round((first + last) / 2), last])]) {
        chart.append(svg('text', { x: String(x(turn)), y: '254', class: 'series-axis-label', 'text-anchor': 'middle' }, `Turn ${turn}`));
    }
    visibleSeries.forEach((series, seriesIndex) => {
        const segments = [];
        for (const point of series.points) {
            const segment = segments.at(-1);
            if (!segment?.length || point.turn !== segment.at(-1).turn + 1)
                segments.push([point]);
            else
                segment.push(point);
        }
        for (const segment of segments) {
            const commands = segment.map((point, index) => `${index ? 'L' : 'M'} ${x(point.turn)} ${y(point.value)}`).join(' ');
            const path = segment.length === 1 ? `${commands} l 1 0` : commands;
            chart.append(svg('path', { d: path, class: `turn-series-line series-${seriesIndex % 4}`, 'data-series-id': series.id }));
        }
        for (const point of series.points) {
            const hit = svg('rect', { x: String(x(point.turn) - 7), y: String(y(point.value) - 11), width: '14', height: '22',
                class: 'turn-series-hit', 'data-series-point': '', 'data-series-id': series.id, 'data-turn': String(point.turn),
                'aria-label': `${series.label}, turn ${point.turn}, ${formatValue(point.value)}` });
            chart.append(hit);
        }
    });
    for (const turn of new Set([first, last])) {
        chart.append(svg('line', { x1: String(x(turn)), x2: String(x(turn)), y1: String(top), y2: String(bottom), class: 'turn-series-selection' }));
    }
    if (!values.length)
        chart.append(svg('text', { x: String((left + right) / 2), y: '125', class: 'series-empty', 'text-anchor': 'middle' }, 'No recorded values in this range'));
    const primary = selectedSeries[0];
    el('turn-series-title').textContent = select.value === 'reward' ? 'Participant Reward by Turn' : primary?.label ?? 'Recorded Series';
    el('turn-series-unit').textContent = primary?.unit ? `Unit: ${primary.unit}` : primary ? 'Recorded numeric values' : '';
    const recordedTurns = new Set(visibleSeries.flatMap(series => series.points.map(point => point.turn))).size;
    el('turn-series-availability').textContent = visible.length
        ? `Recorded on ${recordedTurns} of ${visible.length} turns. Missing values remain gaps.` : 'No recorded turns.';
    const legend = el('turn-series-legend');
    legend.replaceChildren();
    for (const series of visibleSeries)
        legend.append(text('span', series.label));
    const markerHolder = el('turn-series-markers');
    markerHolder.replaceChildren();
    const revisionTurns = new Map(turns.map((turn, index) => [turn.committed?.revision, index + 1]));
    const markers = [];
    for (const event of events) {
        const turn = revisionTurns.get(event.revision);
        if (!turn || turn < first || turn > last)
            continue;
        if (event.kind === 'checkpoint.committed')
            markers.push({ turn, label: 'Checkpoint' });
        if (event.kind === 'report')
            markers.push({ turn, label: 'Score Report' });
    }
    for (const [index, turn] of turns.entries()) {
        if (index + 1 < first || index + 1 > last)
            continue;
        if (turn.committed?.terminated)
            markers.push({ turn: index + 1, label: 'Terminated' });
        else if (turn.committed?.truncated)
            markers.push({ turn: index + 1, label: 'Truncated' });
    }
    for (const series of findingSeries) {
        for (const point of series.points)
            if (point.turn >= first && point.turn <= last) {
                markers.push({ turn: point.turn, label: `${series.label} (${formatValue(point.value)})` });
            }
    }
    if (markers.length)
        for (const marker of markers)
            markerHolder.append(text('span', `Turn ${marker.turn} · ${marker.label}`));
    else
        markerHolder.append(text('span', 'No score reports, checkpoints, or terminal events in this range.', 'muted'));
    const difference = el('turn-difference');
    difference.replaceChildren();
    if (!turns.length) {
        difference.append(text('p', 'No turns are available in this environment session.', 'muted'));
        return;
    }
    const turnA = first, turnB = last;
    if (turnA === turnB) {
        difference.append(text('h3', `Turn ${turnA} is the only turn in view`));
        const valuesPanel = text('div', '', 'turn-difference-values');
        for (const series of selectedSeries) {
            const value = series.points.find(point => point.turn === turnA)?.value;
            const row = text('section', '', 'turn-difference-series single-turn');
            row.append(text('strong', series.label), text('span', value === undefined ? 'Not recorded' : formatValue(value)));
            valuesPanel.append(row);
        }
        difference.append(valuesPanel);
        return;
    }
    difference.append(text('h3', `Change from Turn ${turnA} to Turn ${turnB}`));
    const valuesPanel = text('div', '', 'turn-difference-values');
    for (const series of selectedSeries) {
        const firstValue = series.points.find(point => point.turn === turnA)?.value;
        const lastValue = series.points.find(point => point.turn === turnB)?.value;
        const row = text('section', '', 'turn-difference-series');
        row.append(text('strong', series.label), text('span', `Turn ${turnA}\n${firstValue === undefined ? 'Not recorded' : formatValue(firstValue)}`), text('span', `Turn ${turnB}\n${lastValue === undefined ? 'Not recorded' : formatValue(lastValue)}`), text('span', `Change\n${firstValue === undefined || lastValue === undefined ? 'Not available' : formatValue(lastValue - firstValue)}`));
        valuesPanel.append(row);
    }
    difference.append(valuesPanel);
    const evidenceChanges = text('section', '', 'turn-evidence-changes');
    evidenceChanges.append(text('h4', 'Participant Evidence Changes'));
    const before = turns[turnA - 1], after = turns[turnB - 1];
    let changed = 0;
    for (const name of environment?.participants ?? []) {
        const fields = SLOTS.filter(slot => slotValue(before, name, slot) !== slotValue(after, name, slot));
        if (!fields.length)
            continue;
        changed += 1;
        const row = text('div', '', 'turn-evidence-change');
        row.append(text('strong', participantName(name)), text('span', fields.map(slot => `${slot === 'attempted' ? 'Action' : participantName(slot)} changed`).join(' · ')));
        evidenceChanges.append(row);
    }
    if (!changed)
        evidenceChanges.append(text('p', 'No participant evidence changed between these turns.', 'muted'));
    difference.append(evidenceChanges);
}
function renderCompareSessionList() {
    const holder = el('compare-session-list');
    holder.replaceChildren();
    for (const item of catalog) {
        const label = text('label', '', 'compare-session-option');
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = selected.has(item.id);
        const copy = text('span', sessionTitle(item));
        copy.append(text('small', `${sessionStatus(item.status)} · ${item.revision} turns`, 'muted'));
        check.onchange = () => {
            check.checked ? selected.add(item.id) : selected.delete(item.id);
            persistSelection();
            renderCompareSessionList();
            renderModeSummary();
        };
        label.append(check, copy);
        holder.append(label);
    }
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
function evaluationValue(value) {
    if (typeof value === 'number')
        return Number(value.toPrecision(6)).toString();
    if (typeof value === 'string')
        return value;
    if (typeof value === 'boolean')
        return value ? 'Yes' : 'No';
    if (value === null)
        return 'None';
    return Array.isArray(value) ? `${value.length} Items` : `${Object.keys(value).length} Fields`;
}
function renderReports(value) {
    const holder = el('reports');
    holder.replaceChildren();
    if (!value.length) {
        holder.append(text('p', 'No scores or findings recorded.', 'muted'));
        return;
    }
    const summary = summarizeReports(events, environment?.participants ?? [], value);
    const context = text('p', '', 'evaluation-context');
    context.textContent = `${summary.latestTurn ? `Evaluated through Turn ${summary.latestTurn}` : 'Evaluation recorded'}${summary.scorers.length ? ` · ${summary.scorers.join(', ')}` : ''}`;
    holder.append(context);
    const metrics = text('section', '', 'evaluation-metrics');
    metrics.id = 'evaluation-metrics';
    const heading = text('div', '', 'evaluation-metric evaluation-metric-heading');
    for (const label of ['Metric', 'Start', 'Latest', 'Change'])
        heading.append(text('span', label));
    metrics.append(heading);
    for (const metric of summary.metrics) {
        const row = text('div', '', 'evaluation-metric');
        const name = text('span', '', 'evaluation-metric-name');
        name.append(text('strong', metric.label), text('small', [metric.unit, `${metric.scorer}@${metric.version}`].filter(Boolean).join(' · ')));
        row.append(name, text('span', evaluationValue(metric.first)), text('span', evaluationValue(metric.latest)), text('span', metric.change === null ? '—' : `${metric.change > 0 ? '+' : ''}${evaluationValue(metric.change)}`));
        metrics.append(row);
    }
    if (summary.metrics.length)
        holder.append(metrics);
    else
        holder.append(text('p', 'No metrics recorded.', 'muted'));
    const findings = text('section', '', 'evaluation-findings');
    findings.append(text('h3', 'Findings'));
    if (!summary.findings.length)
        findings.append(text('p', 'No findings recorded.', 'muted'));
    for (const finding of summary.findings) {
        const row = text('article', '', 'evaluation-finding');
        row.append(text('strong', `${participantName(finding.category)} · ${participantName(finding.status)}`), text('span', [finding.turn ? `Turn ${finding.turn}` : 'Turn unavailable', participantName(finding.participant), finding.rule].join(' · ')), text('p', finding.judgment));
        findings.append(row);
    }
    holder.append(findings);
    if (summary.uncertainties.length)
        holder.append(text('p', summary.uncertainties.at(-1), 'evaluation-uncertainty'));
}
function renderReportHistory(value) {
    const holder = el('report-history');
    holder.replaceChildren();
    if (!value.length) {
        holder.append(text('p', 'No reports recorded.', 'muted'));
        return;
    }
    const records = [...value].sort((a, b) => b.revision - a.revision);
    const latest = records[0];
    const latestSection = text('section', '', 'report-latest');
    const latestHeading = text('div', '', 'comparison-chart-heading');
    latestHeading.append(text('h3', 'Latest Report'), text('p', `Revision ${latest.revision} · Evidence ${latest.report.evidence_cursor}`));
    latestSection.append(latestHeading);
    const latestCard = text('article', '', 'report-latest-card');
    const facts = text('dl', '', 'comparison-session-facts');
    for (const [label, factValue] of [
        ['Revision', String(latest.revision)],
        ['Evidence Cursor', String(latest.report.evidence_cursor)],
        ['Scorer', `${latest.report.scorer}@${latest.report.version}`],
        ['Evaluator', participantName(latest.report.kind)],
        ['Metrics', String(Object.keys(latest.report.metrics ?? {}).length)],
        ['Findings', String(latest.report.findings?.length ?? 0)],
    ]) {
        const fact = text('div', '');
        fact.append(text('dt', label), text('dd', factValue));
        facts.append(fact);
    }
    latestCard.append(facts);
    const latestMetrics = text('div', '', 'report-latest-metrics');
    for (const [key, metric] of Object.entries(latest.report.metrics ?? {})) {
        const row = text('div', '', 'metric');
        row.append(text('strong', evaluationValue(metric)), text('span', participantName(key)));
        latestMetrics.append(row);
    }
    if (latestMetrics.childElementCount)
        latestCard.append(latestMetrics);
    if (latest.report.uncertainty) {
        const uncertainty = text('div', '', 'report-latest-uncertainty');
        uncertainty.append(text('strong', 'Uncertainty'), text('p', latest.report.uncertainty));
        latestCard.append(uncertainty);
    }
    latestSection.append(latestCard);
    holder.append(latestSection);
    renderReportProgression(holder, records);
    const historyHeading = text('header', '', 'report-history-heading');
    historyHeading.append(text('h3', 'Revision History'), text('p', `${records.length} versioned ${records.length === 1 ? 'report' : 'reports'} · newest first`));
    holder.append(historyHeading);
    const history = text('div', '', 'report-revision-list');
    for (const record of records) {
        const block = document.createElement('details');
        block.className = 'report-record';
        const summary = text('summary', '', 'report-record-summary');
        const heading = text('span', '', 'report-record-heading');
        heading.append(text('strong', `Revision ${record.revision}`), text('small', `${record.report.scorer}@${record.report.version} · Evidence ${record.report.evidence_cursor}`));
        const summaryMetrics = text('span', '', 'report-record-summary-metrics');
        for (const [key, metric] of Object.entries(record.report.metrics ?? {})) {
            summaryMetrics.append(text('span', `${participantName(key)} ${evaluationValue(metric)}`));
        }
        summary.append(heading, summaryMetrics);
        block.append(summary);
        const body = text('div', '', 'report-record-body');
        const recordFacts = text('dl', '', 'comparison-session-facts report-record-facts');
        for (const [label, factValue] of [
            ['Evaluator', participantName(record.report.kind)],
            ['Metrics', String(Object.keys(record.report.metrics ?? {}).length)],
            ['Rewards', String(Object.keys(record.report.rewards ?? {}).length)],
            ['Findings', String(record.report.findings?.length ?? 0)],
            ['Evidence Cursor', String(record.report.evidence_cursor)],
            ['Hash', shortId(record.hash)],
        ]) {
            const fact = text('div', '');
            fact.append(text('dt', label), text('dd', factValue));
            recordFacts.append(fact);
        }
        body.append(recordFacts);
        if (record.report.findings?.length) {
            const findings = text('section', '', 'report-record-findings');
            findings.append(text('h4', 'Findings'));
            for (const finding of record.report.findings) {
                const row = text('article', '', 'evaluation-finding');
                row.append(text('strong', `${participantName(finding.category)} · ${participantName(finding.status)}`), text('span', `${participantName(finding.participant)} · ${finding.rule}`), text('p', finding.judgment));
                findings.append(row);
            }
            body.append(findings);
        }
        if (record.report.uncertainty) {
            const uncertainty = text('div', '', 'report-record-uncertainty');
            uncertainty.append(text('strong', 'Uncertainty'), text('p', record.report.uncertainty));
            body.append(uncertainty);
        }
        body.append(details('Raw', text('pre', json(record))));
        block.append(body);
        history.append(block);
    }
    holder.append(history);
}
function renderReportProgression(holder, recordsNewestFirst) {
    const records = [...recordsNewestFirst].reverse();
    const series = new Map();
    for (const record of records) {
        for (const [key, value] of Object.entries(record.report.metrics ?? {})) {
            if (typeof value !== 'number' || !Number.isFinite(value))
                continue;
            const points = series.get(key) ?? [];
            points.push({ revision: record.revision, value });
            series.set(key, points);
        }
    }
    if (!series.size)
        return;
    const section = text('section', '', 'comparison-visualization report-progression');
    const heading = text('header', '', 'comparison-visualization-heading');
    const copy = text('div', '');
    copy.append(text('h3', 'Report Progression'), text('p', 'Numeric metric values across versioned evaluation reports.'));
    heading.append(copy, text('p', `${series.size} ${series.size === 1 ? 'metric' : 'metrics'} · ${records.length} revisions`, 'muted'));
    section.append(heading);
    const progression = text('section', '', 'comparison-progression report-progression-body');
    const progressionHeading = text('div', '', 'comparison-chart-heading');
    progressionHeading.append(text('h4', 'Progression by Revision'), text('p', 'Each metric uses its own scale so changes remain readable.'));
    progression.append(progressionHeading);
    const grid = text('div', '', 'comparison-progression-grid');
    let chartIndex = 0;
    for (const [key, points] of series) {
        const chart = text('article', '', 'comparison-line-card report-line-card');
        const definition = [...records].reverse().find(record => record.report.metric_definitions?.[key])?.report.metric_definitions?.[key];
        chart.append(text('h5', participantName(key)));
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        const titleId = `report-chart-${chartIndex++}`;
        svg.setAttribute('viewBox', '0 0 420 190');
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-labelledby', titleId);
        const svgNode = (tag, attributes, value = '') => {
            const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
            for (const [name, attribute] of Object.entries(attributes))
                node.setAttribute(name, attribute);
            if (value)
                node.textContent = value;
            return node;
        };
        svg.append(svgNode('title', { id: titleId }, `${participantName(key)} progression by report revision`));
        const left = 42, right = 405, top = 14, bottom = 154;
        const minimumRevision = Math.min(...points.map(point => point.revision));
        const maximumRevision = Math.max(...points.map(point => point.revision));
        let minimum = Math.min(...points.map(point => point.value));
        let maximum = Math.max(...points.map(point => point.value));
        if (minimum === maximum) {
            minimum -= Math.abs(minimum || 1) * .1;
            maximum += Math.abs(maximum || 1) * .1;
        }
        const x = (revision) => left + ((revision - minimumRevision) / Math.max(1, maximumRevision - minimumRevision)) * (right - left);
        const y = (metric) => bottom - ((metric - minimum) / Math.max(Number.EPSILON, maximum - minimum)) * (bottom - top);
        for (const ratio of [0, .5, 1]) {
            const lineY = bottom - ratio * (bottom - top);
            svg.append(svgNode('line', { x1: String(left), x2: String(right), y1: String(lineY), y2: String(lineY), class: 'comparison-grid-line' }));
            svg.append(svgNode('text', { x: String(left - 8), y: String(lineY + 4), class: 'comparison-axis-label', 'text-anchor': 'end' }, chartValue(minimum + ratio * (maximum - minimum))));
        }
        for (const revision of [...new Set([minimumRevision, Math.round((minimumRevision + maximumRevision) / 2), maximumRevision])]) {
            svg.append(svgNode('text', { x: String(x(revision)), y: '178', class: 'comparison-axis-label', 'text-anchor': 'middle' }, `R${revision}`));
        }
        const path = points.map((point, index) => `${index ? 'L' : 'M'} ${x(point.revision)} ${y(point.value)}`).join(' ');
        if (path)
            svg.append(svgNode('path', { d: points.length === 1 ? `${path} l 1 0` : path, class: 'comparison-line' }));
        for (const point of points) {
            svg.append(svgNode('circle', { cx: String(x(point.revision)), cy: String(y(point.value)), r: '3.5', tabindex: '0',
                class: 'comparison-point', 'aria-label': `Revision ${point.revision}, ${chartValue(point.value)}` }));
        }
        chart.append(svg);
        const legend = text('div', '', 'comparison-line-legend');
        const legendItem = text('span', '');
        legendItem.append(text('i', ''), document.createTextNode(`${points.length} reported revisions`));
        legend.append(legendItem, text('small', `Latest ${chartValue(points.at(-1).value)}${definition?.unit ? ` ${definition.unit}` : ''}`));
        chart.append(legend);
        grid.append(chart);
    }
    progression.append(grid);
    section.append(progression);
    holder.append(section);
}
const comparisonMetricDefinitions = [
    { id: 'synthetic-total', label: 'Synthetic Total', unit: 'count' },
    { id: 'cumulative-reward', label: 'Cumulative Reward', unit: 'reward' },
    { id: 'executed-actions', label: 'Executed Actions', unit: 'count' },
];
function comparisonSessionSeries(item, projection) {
    const title = sessionTitle({ participants: item.participants, parent: item.parent, id: item.environment });
    const grouped = title.match(/^(.*) · Trial (\d+)$/);
    const series = projection?.series ?? [];
    const metric = (id) => {
        const match = series.find(candidate => candidate.id === id);
        return {
            points: match?.points.map(point => ({ turn: point.turn, value: point.value })) ?? [],
            sourcePoints: match?.source_points ?? 0,
            downsampled: match?.downsampled ?? false,
        };
    };
    return {
        environment: item.environment,
        title,
        scenario: grouped?.[1] ?? title,
        trial: grouped ? `Trial ${grouped[2]}` : shortId(item.environment),
        totalTurns: projection?.total_turns ?? item.revision ?? 0,
        metrics: {
            'synthetic-total': metric('signal:synthetic.total:total'),
            'cumulative-reward': metric('reward:cumulative'),
            'executed-actions': metric('activity:executed:cumulative'),
        },
    };
}
const chartValue = (value) => Number(value.toPrecision(6)).toString();
function renderComparisonVisualizations(holder, initialSessions, loadRange, initialView, onViewChange) {
    let sessions = initialSessions;
    const available = comparisonMetricDefinitions.filter(metric => sessions.some(session => session.metrics[metric.id].points.length));
    if (!available.length)
        return;
    const finalValue = (session, metric) => session.metrics[metric].points.at(-1)?.value;
    const defaultMetric = [...available].sort((a, b) => new Set(sessions.map(session => finalValue(session, b.id)).filter(value => value !== undefined)).size
        - new Set(sessions.map(session => finalValue(session, a.id)).filter(value => value !== undefined)).size)[0];
    let selectedMetric = initialView.metric && available.some(metric => metric.id === initialView.metric)
        ? initialView.metric : defaultMetric.id;
    const section = text('section', '', 'comparison-visualization');
    const heading = text('header', '', 'comparison-visualization-heading');
    const copy = text('div', '');
    copy.append(text('h3', 'Session Progression'), text('p', 'Compare final values and turn-by-turn evidence across the selected environment sessions.'));
    const controls = text('div', '', 'comparison-metric-controls');
    controls.setAttribute('role', 'group');
    controls.setAttribute('aria-label', 'Comparison metric');
    const body = text('div', '', 'comparison-visualization-body');
    const maximumTurn = Math.max(...initialSessions.map(session => session.totalTurns), 1);
    let rangeStart = Math.min(maximumTurn, initialView.startTurn ?? 1);
    let rangeEnd = Math.max(rangeStart, Math.min(maximumTurn, initialView.endTurn ?? maximumTurn));
    let rangeRequest = 0;
    const range = text('div', '', 'turn-range-controls comparison-turn-range');
    const rangeLabels = text('div', '', 'turn-range-labels');
    const rangeCopy = text('div', '');
    rangeCopy.append(text('span', 'Visible Range'), text('p', `${rangeEnd - rangeStart + 1} of ${maximumTurn} turns in view`));
    const rangeSummary = rangeCopy.querySelector('p');
    const rangeActions = text('div', '', 'comparison-turn-range-actions');
    const rangeValues = text('dl', '', 'turn-range-values');
    const rangeValue = (label, value) => {
        const holder = text('div', '');
        const output = text('dd', `Turn ${value}`);
        holder.append(text('dt', label), output);
        return { holder, output };
    };
    const startValue = rangeValue('Start', rangeStart), endValue = rangeValue('End', rangeEnd);
    rangeValues.append(startValue.holder, endValue.holder);
    const resetRange = text('button', 'Full Range', 'text-button');
    resetRange.type = 'button';
    rangeActions.append(rangeValues, resetRange);
    rangeLabels.append(rangeCopy, rangeActions);
    const rangeTrack = text('div', '', 'turn-range-track');
    const rangeSelection = text('div', '', 'turn-range-selection');
    rangeSelection.title = 'Drag or scroll to move the selected turn range';
    const rangeInput = (label, value) => {
        const input = document.createElement('input');
        input.type = 'range';
        input.min = '1';
        input.max = String(maximumTurn);
        input.value = String(value);
        input.setAttribute('aria-label', `${label} turn`);
        return input;
    };
    const startControl = rangeInput('Start', rangeStart), endControl = rangeInput('End', rangeEnd);
    rangeTrack.append(rangeSelection, startControl, endControl);
    range.append(rangeLabels, rangeTrack);
    range.hidden = maximumTurn <= 1;
    const updateRangeLabels = () => {
        startValue.output.textContent = `Turn ${rangeStart}`;
        endValue.output.textContent = `Turn ${rangeEnd}`;
        rangeSummary.textContent = `${rangeEnd - rangeStart + 1} of ${maximumTurn} turns in view`;
        const scale = Math.max(1, maximumTurn - 1);
        rangeSelection.style.left = `${((rangeStart - 1) / scale) * 100}%`;
        rangeSelection.style.width = `${((rangeEnd - rangeStart) / scale) * 100}%`;
        rangeSelection.dataset.first = String(rangeStart);
        rangeSelection.dataset.last = String(rangeEnd);
        rangeSelection.dataset.maximum = String(maximumTurn);
    };
    updateRangeLabels();
    const render = () => {
        for (const button of controls.querySelectorAll('button')) {
            button.setAttribute('aria-pressed', String(button.dataset.metric === selectedMetric));
        }
        body.replaceChildren();
        const metric = comparisonMetricDefinitions.find(candidate => candidate.id === selectedMetric);
        const visible = sessions.filter(session => session.metrics[selectedMetric].points.length);
        if (!visible.length) {
            body.append(text('p', 'No turn-level evidence is available for this metric.', 'muted'));
            return;
        }
        const allPoints = visible.flatMap(session => session.metrics[selectedMetric].points);
        const finalValues = visible.map(session => finalValue(session, selectedMetric)).filter(Number.isFinite);
        let minimum = Math.min(...finalValues), maximum = Math.max(...finalValues);
        if (minimum === maximum) {
            minimum -= Math.abs(minimum || 1) * .1;
            maximum += Math.abs(maximum || 1) * .1;
        }
        const span = Math.max(Number.EPSILON, maximum - minimum);
        const finalPlot = text('section', '', 'comparison-final-plot');
        const finalHeading = text('div', '', 'comparison-chart-heading');
        finalHeading.append(text('h4', 'Latest Values in Range'), text('p', `${metric.label} · ${metric.unit}`));
        finalPlot.append(finalHeading);
        const axis = text('div', '', 'comparison-dot-axis');
        axis.append(text('span', chartValue(minimum)), text('span', chartValue((minimum + maximum) / 2)), text('span', chartValue(maximum)));
        finalPlot.append(axis);
        const scenarios = new Map();
        for (const session of visible) {
            const group = scenarios.get(session.scenario) ?? [];
            group.push(session);
            scenarios.set(session.scenario, group);
        }
        for (const [scenario, group] of scenarios) {
            const row = text('div', '', 'comparison-dot-row');
            row.append(text('strong', scenario, 'comparison-dot-label'));
            const track = text('div', '', 'comparison-dot-track');
            track.style.minHeight = `${Math.max(44, group.length * 25 + 10)}px`;
            group.forEach((session, index) => {
                const value = finalValue(session, selectedMetric);
                const position = (value - minimum) / span;
                const marker = text('span', '', 'comparison-dot-marker');
                marker.style.left = `${position * 100}%`;
                marker.style.top = `${16 + index * 25}px`;
                if (position > .72)
                    marker.classList.add('is-end');
                marker.setAttribute('aria-label', `${session.title}: ${chartValue(value)} ${metric.unit}`);
                marker.append(text('span', '', `comparison-dot trial-${index % 4}`), text('small', `${session.trial} · ${chartValue(value)}`));
                track.append(marker);
            });
            row.append(track);
            finalPlot.append(row);
        }
        body.append(finalPlot);
        let seriesMinimum = Math.min(...allPoints.map(point => point.value));
        let seriesMaximum = Math.max(...allPoints.map(point => point.value));
        if (seriesMinimum === seriesMaximum) {
            seriesMinimum -= Math.abs(seriesMinimum || 1) * .1;
            seriesMaximum += Math.abs(seriesMaximum || 1) * .1;
        }
        const progression = text('section', '', 'comparison-progression');
        const progressionHeading = text('div', '', 'comparison-chart-heading');
        const lengths = new Set(visible.map(session => session.totalTurns));
        progressionHeading.append(text('h4', 'Progression by Turn'), text('p', lengths.size > 1
            ? `Shared axes through Turn ${maximumTurn}; shorter lines end with their sessions.`
            : 'Each panel uses the same axes for direct comparison.'));
        progression.append(progressionHeading);
        const grid = text('div', '', 'comparison-progression-grid');
        let chartIndex = 0;
        for (const [scenario, group] of scenarios) {
            const chart = text('article', '', 'comparison-line-card');
            chart.append(text('h5', scenario));
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            const titleId = `comparison-chart-${chartIndex++}`;
            svg.setAttribute('viewBox', '0 0 420 190');
            svg.setAttribute('role', 'img');
            svg.setAttribute('aria-labelledby', titleId);
            const svgNode = (tag, attributes, value = '') => {
                const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
                for (const [key, attribute] of Object.entries(attributes))
                    node.setAttribute(key, attribute);
                if (value)
                    node.textContent = value;
                return node;
            };
            svg.append(svgNode('title', { id: titleId }, `${metric.label} progression for ${scenario}`));
            const left = 42, right = 405, top = 14, bottom = 154;
            const x = (turn) => left + ((turn - rangeStart) / Math.max(1, rangeEnd - rangeStart)) * (right - left);
            const y = (value) => bottom - ((value - seriesMinimum) / Math.max(Number.EPSILON, seriesMaximum - seriesMinimum)) * (bottom - top);
            for (const ratio of [0, .5, 1]) {
                const lineY = bottom - ratio * (bottom - top);
                svg.append(svgNode('line', { x1: String(left), x2: String(right), y1: String(lineY), y2: String(lineY), class: 'comparison-grid-line' }));
                svg.append(svgNode('text', { x: String(left - 8), y: String(lineY + 4), class: 'comparison-axis-label', 'text-anchor': 'end' }, chartValue(seriesMinimum + ratio * (seriesMaximum - seriesMinimum))));
            }
            for (const turn of [...new Set([rangeStart, Math.round((rangeStart + rangeEnd) / 2), rangeEnd])]) {
                svg.append(svgNode('text', { x: String(x(turn)), y: '178', class: 'comparison-axis-label', 'text-anchor': 'middle' }, `T${turn}`));
            }
            group.forEach((session, index) => {
                const points = session.metrics[selectedMetric].points;
                const path = points.map((point, pointIndex) => `${pointIndex ? 'L' : 'M'} ${x(point.turn)} ${y(point.value)}`).join(' ');
                if (path)
                    svg.append(svgNode('path', { d: points.length === 1 ? `${path} l 1 0` : path, class: `comparison-line trial-${index % 4}` }));
                for (const point of points) {
                    const dot = svgNode('circle', { cx: String(x(point.turn)), cy: String(y(point.value)), r: '3.5', tabindex: '0',
                        class: `comparison-point trial-${index % 4}`, 'aria-label': `${session.trial}, turn ${point.turn}, ${chartValue(point.value)}` });
                    svg.append(dot);
                }
            });
            chart.append(svg);
            const legend = text('div', '', 'comparison-line-legend');
            group.forEach((session, index) => {
                const item = text('span', '');
                item.append(text('i', '', `trial-${index % 4}`), document.createTextNode(`${session.trial} · ${session.totalTurns} turns`));
                legend.append(item);
            });
            const signatures = group.map(session => JSON.stringify(session.metrics[selectedMetric].points.map(point => [point.turn, point.value])));
            if (new Set(signatures).size < signatures.length)
                legend.append(text('small', 'Overlapping trials share the same values.'));
            const sampled = group.filter(session => session.metrics[selectedMetric].downsampled);
            if (sampled.length)
                legend.append(text('small', `Extrema preserved from up to ${Math.max(...sampled.map(session => session.metrics[selectedMetric].sourcePoints))} recorded points.`));
            chart.append(legend);
            grid.append(chart);
        }
        progression.append(grid);
        body.append(progression);
    };
    for (const metric of available) {
        const button = text('button', metric.label, 'comparison-metric-button');
        button.type = 'button';
        button.dataset.metric = metric.id;
        button.onclick = () => {
            selectedMetric = metric.id;
            onViewChange({ metric: selectedMetric, startTurn: rangeStart, endTurn: rangeEnd });
            render();
        };
        controls.append(button);
    }
    const refreshRange = () => void attempt(async () => {
        rangeStart = Math.min(Number(startControl.value), Number(endControl.value));
        rangeEnd = Math.max(Number(startControl.value), Number(endControl.value));
        startControl.value = String(rangeStart);
        endControl.value = String(rangeEnd);
        updateRangeLabels();
        onViewChange({ metric: selectedMetric, startTurn: rangeStart, endTurn: rangeEnd });
        const request = ++rangeRequest;
        section.setAttribute('aria-busy', 'true');
        try {
            const loaded = await loadRange(rangeStart, rangeEnd);
            if (request !== rangeRequest)
                return;
            sessions = loaded;
            render();
        }
        finally {
            if (request === rangeRequest)
                section.removeAttribute('aria-busy');
        }
    });
    const previewRange = () => {
        rangeStart = Math.min(Number(startControl.value), Number(endControl.value));
        rangeEnd = Math.max(Number(startControl.value), Number(endControl.value));
        startControl.value = String(rangeStart);
        endControl.value = String(rangeEnd);
        updateRangeLabels();
    };
    startControl.oninput = previewRange;
    endControl.oninput = previewRange;
    startControl.onchange = refreshRange;
    endControl.onchange = refreshRange;
    bindTurnRangeTrack(rangeTrack, rangeSelection, startControl, endControl, previewRange, refreshRange);
    resetRange.onclick = () => {
        startControl.value = '1';
        endControl.value = String(maximumTurn);
        refreshRange();
    };
    heading.append(copy, controls);
    section.append(heading, range, body);
    holder.append(section);
    render();
}
function renderComparison(value, sessions, loadRange, initialView, onViewChange) {
    const result = value;
    const holder = el('compare-results');
    holder.replaceChildren();
    const cards = text('div', '', 'comparison-cards');
    const seriesByEnvironment = new Map(sessions.map(session => [session.environment, session]));
    for (const item of result.environments) {
        const session = seriesByEnvironment.get(item.environment);
        const card = text('section', '', 'comparison-card');
        const button = text('button', sessionTitle({ participants: item.participants ?? known(item.environment)?.participants, parent: item.parent, id: item.environment }), 'link');
        button.setAttribute('aria-current', String(environment?.id === item.environment));
        button.onclick = () => void attempt(() => attach(item.environment));
        card.append(button);
        card.append(text('p', shortId(item.environment), 'comparison-session-id muted'));
        const facts = text('dl', '', 'comparison-session-facts');
        for (const [label, value] of [
            ['Total Turns', String(session?.totalTurns ?? 'Unknown')],
            ['Participants', String(item.participants?.length ?? known(item.environment)?.participants.length ?? 'Unknown')],
            ['Status', sessionStatus(item.status ?? 'unknown')],
            ['Evidence Revision', String(item.revision ?? 'Unknown')],
            ['Recorded Cost', formatCost(item.cost_micros)],
            ['Lineage', shortId(item.lineage)],
        ]) {
            const fact = text('div', '');
            fact.append(text('dt', label), text('dd', value));
            facts.append(fact);
        }
        card.append(facts);
        for (const [key, changed] of Object.entries(item.interventions ?? {}))
            card.append(text('p', `${key.replaceAll('_', ' ')} set to ${typeof changed === 'object' ? JSON.stringify(changed) : String(changed)}.`, 'muted'));
        const metrics = item.latest_report?.report.metrics ?? {};
        for (const [key, metric] of Object.entries(metrics)) {
            const row = text('div', '', 'metric');
            row.append(text('strong', typeof metric === 'object' ? JSON.stringify(metric) : String(metric)), text('span', key.replaceAll('_', ' ')));
            card.append(row);
        }
        if (!Object.keys(metrics).length)
            card.append(text('p', 'No Score Report Recorded', 'muted'));
        cards.append(card);
    }
    holder.append(cards);
    const turnCounts = sessions.map(session => session.totalTurns);
    if (new Set(turnCounts).size > 1) {
        const minimumTurns = Math.min(...turnCounts), maximumTurns = Math.max(...turnCounts);
        const note = text('section', '', 'comparison-length-note');
        note.append(text('strong', 'Different Session Lengths'), text('p', `${minimumTurns}–${maximumTurns} recorded turns across these environment sessions. Charts use one shared turn axis; shorter sessions end at their actual final turn and are never padded.`));
        holder.append(note);
    }
    renderComparisonVisualizations(holder, sessions, loadRange, initialView, onViewChange);
    renderComparisonReports(holder, result);
}
function renderComparisonReports(holder, result) {
    const groups = result.metric_groups ?? [];
    const section = text('section', '', 'comparison-reports');
    const heading = text('header', '', 'comparison-reports-heading');
    const copy = text('div', '');
    copy.append(text('h3', 'Comparison Reports'), text('p', 'Aggregate score reports grouped by compatible scorer, version, metric definition, and cohort.'));
    heading.append(copy, text('p', `${groups.length} compatible ${groups.length === 1 ? 'metric group' : 'metric groups'}`, 'muted'));
    section.append(heading);
    if (result.uncertainty || result.design) {
        const context = text('div', '', 'comparison-report-context');
        if (result.uncertainty) {
            const item = text('div', '');
            item.append(text('strong', 'Uncertainty'), text('p', result.uncertainty));
            context.append(item);
        }
        if (result.design) {
            const item = text('div', '');
            item.append(text('strong', 'Comparison Basis'), text('p', result.design));
            context.append(item);
        }
        section.append(context);
    }
    if (!groups.length)
        section.append(text('p', 'No compatible score report metrics were available.', 'muted'));
    const grid = text('div', '', 'comparison-report-grid');
    for (const group of groups) {
        const card = text('article', '', 'comparison-report-card');
        const cardHeading = text('header', '', 'comparison-report-card-heading');
        const identity = text('div', '');
        identity.append(text('h4', participantName(group.metric)), text('p', `${group.scorer}@${group.version} · ${participantName(group.kind)}`, 'muted'));
        cardHeading.append(identity, text('span', shortId(group.id), 'tag'));
        card.append(cardHeading);
        const aggregate = text('div', '', 'metric comparison-report-aggregate');
        aggregate.append(text('strong', group.summary ? chartValue(group.summary.mean_of_lineage_means) : 'Raw values only'), text('span', group.summary ? 'Mean of lineage means' : 'No compatible aggregate'));
        card.append(aggregate);
        const facts = text('dl', '', 'comparison-session-facts comparison-report-facts');
        for (const [label, factValue] of [
            ['Reported', `${group.reported_environments}/${group.selected_environments}`],
            ['Missing', String(group.missing_environments)],
            ['Incomplete', String(group.incomplete_environments)],
            ['Lineages', group.summary ? String(group.summary.independent_lineages) : 'Not available'],
            ['Standard Error', group.summary?.standard_error === null || !group.summary ? 'Not available' : chartValue(group.summary.standard_error)],
            ['Unit', group.definition?.unit ?? 'Unspecified'],
        ]) {
            const fact = text('div', '');
            fact.append(text('dt', label), text('dd', factValue));
            facts.append(fact);
        }
        card.append(facts);
        const disclosure = document.createElement('details');
        disclosure.className = 'comparison-report-details';
        disclosure.append(text('summary', 'Session values and report revisions'));
        const values = text('div', '', 'comparison-report-values');
        const valuesHeading = text('div', '', 'comparison-report-value comparison-report-value-heading');
        for (const label of ['Environment Session', 'Value', 'Revision', 'Status', 'Lineage'])
            valuesHeading.append(text('span', label));
        values.append(valuesHeading);
        for (const value of group.values) {
            const row = text('div', '', 'comparison-report-value');
            row.append(text('span', shortId(value.environment)), text('strong', chartValue(value.value)), text('span', String(value.report_revision)), text('span', sessionStatus(value.status)), text('span', shortId(value.lineage)));
            values.append(row);
        }
        if (!group.values.length)
            values.append(text('p', 'No environment-session values were reported.', 'muted'));
        disclosure.append(values, details('Raw metric group', text('pre', json(group))));
        card.append(disclosure);
        grid.append(card);
    }
    if (grid.childElementCount)
        section.append(grid);
    if (result.warnings?.length) {
        const warnings = text('div', '', 'comparison-report-warnings');
        warnings.append(text('strong', 'Warnings'));
        for (const warning of result.warnings)
            warnings.append(text('p', warning));
        section.append(warnings);
    }
    holder.append(section);
}
// Connection
async function connect(token) {
    client = new EnvironmentClient(location.origin, token, true);
    await client.request('GET', '/v1/environment');
    activeCredential = token;
    el('token').value = '';
    el('access').hidden = true;
    el('site-header').hidden = true;
    el('local-access-error').hidden = true;
    document.body.classList.add('viewer-connected');
    el('workspace').hidden = false;
    el('connection').textContent = 'Connected';
    try {
        await list();
        scheduleActivity();
    }
    catch {
        message('Participant credentials can attach by environment ID.');
    }
    await restoreRoute();
}
el('connect-form').onsubmit = event => { event.preventDefault(); void attempt(() => connect(el('token').value)); };
async function restoreRoute() {
    const route = routeFromLocation();
    if (!route) {
        showHome(false);
        setLocation('/home', true);
    }
    else if (route.kind === 'session') {
        await attach(route.id, route.tab, false);
        setLocation(sessionPath(route.id, route.tab), true);
    }
    else if (route.kind === 'compare') {
        restoreSelected(route.environments);
        showCompareSessions(false, selected.size < 2);
        if (selected.size >= 2)
            await runCompare([...selected], route.view);
        else
            setLocation(selectionPath('/compare', [...selected], route.view), true);
    }
    else if (route.kind === 'experiment') {
        showExperiment(route.id, false);
        setLocation(`/experiment/${encodeURIComponent(route.id)}`, true);
    }
    else {
        restoreSelected(route.environments);
        showHome(false);
        setLocation(selectionPath('/home', [...selected]), true);
    }
}
async function connectViewer() {
    el('access').hidden = true;
    el('local-access-error').hidden = true;
    try {
        const response = await fetch('/viewer/config', { redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(10000) });
        if (!response.ok)
            throw new Error('Viewer configuration is unavailable.');
        const config = await response.json();
        el('instance-summary').hidden = config.authentication !== 'local';
        if (config.authentication === 'credential') {
            el('access').hidden = false;
            return;
        }
        if (config.authentication !== 'local')
            throw new Error('Viewer authentication mode is unsupported.');
        const connection = await fetch('/local/connect', { method: 'POST', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(10000) });
        if (!connection.ok)
            throw new Error('The local viewer could not establish researcher access.');
        await connect((await connection.json()).token);
    }
    catch {
        el('access').hidden = true;
        el('local-access-error').hidden = false;
    }
}
el('retry-local-access').onclick = () => void connectViewer();
for (const select of document.querySelectorAll('select'))
    initializeDropdown(select);
void connectViewer();
// Wiring
el('attach').onclick = () => void attempt(() => attach(el('environment-id').value.trim()));
el('refresh').onclick = () => void refreshFromButton();
el('empty-refresh').onclick = () => void attempt(refresh);
el('home').onclick = event => {
    event.preventDefault();
    void attempt(async () => {
        selected.clear();
        showHome();
        await list();
    });
};
el('clear-session-selection').onclick = () => { selected.clear(); persistSelection(); renderList(); };
el('session-search').oninput = resetHomePage;
el('session-status').onchange = resetHomePage;
el('session-sort').onchange = resetHomePage;
el('home-page-size').onchange = resetHomePage;
el('home-page-previous').onclick = () => { homePage -= 1; el('home-session-list').scrollTop = 0; renderList(); };
el('home-page-next').onclick = () => { homePage += 1; el('home-session-list').scrollTop = 0; renderList(); };
el('load-more').onclick = () => void attempt(loadEvents);
el('perspective').onchange = renderTimeline;
el('kind').onchange = renderTimeline;
el('export').onclick = () => void attempt(() => download(`/v1/environments/${environment.id}/export`, `${environment.id}.jsonl`));
el('compare').onclick = () => void attempt(async () => {
    showCompareSessions();
    await runCompare([...selected]);
});
el('run-comparison').onclick = () => void attempt(async () => {
    await runCompare([...selected]);
    setComparisonPickerVisible(false);
});
el('copy-id').onclick = () => void attempt(async () => { if (environment) {
    await navigator.clipboard.writeText(environment.id);
    message('Environment ID copied.');
} });
for (const tab of document.querySelectorAll('[data-session-tab]')) {
    tab.onclick = () => void attempt(() => setSessionTab(tab.dataset.sessionTab === 'turns' ? 'turns'
        : tab.dataset.sessionTab === 'progression' ? 'progression'
            : tab.dataset.sessionTab === 'reports' ? 'reports' : 'overview'));
}
el('reports-progression').onclick = () => void attempt(() => setSessionTab('progression'));
el('reports-records').onclick = () => void attempt(() => setSessionTab('reports'));
el('compare-turn-start').oninput = renderProgression;
el('compare-turn-end').oninput = renderProgression;
el('turn-series-select').onchange = renderProgression;
el('turn-series-participant').onchange = renderProgression;
window.onpopstate = () => {
    if (!el('workspace').hidden)
        void attempt(restoreRoute);
};
bindTurnRangeTrack(el('turn-range-track'), el('turn-range-selection'), el('compare-turn-start'), el('compare-turn-end'), renderProgression);
setInterval(() => { if (environment && document.visibilityState === 'visible')
    void attempt(loadEvents); }, 3000);
