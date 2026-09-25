import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';

const [origin, debugPort, trajectoryId, importedTrajectoryId] = process.argv.slice(2);
if (!origin || !debugPort || !trajectoryId || !importedTrajectoryId) {
  throw new Error('origin, debug port, and native and imported trajectory IDs are required');
}

const target = await fetch(
  `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(origin)}`,
  {method: 'PUT'},
).then(response => response.json());
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, {once: true});
  socket.addEventListener('error', reject, {once: true});
});

let commandId = 0;
const pending = new Map();
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (!message.id) return;
  const request = pending.get(message.id);
  if (!request) return;
  pending.delete(message.id);
  if (message.error) request.reject(new Error(message.error.message));
  else request.resolve(message.result);
});

function command(method, params = {}) {
  const id = ++commandId;
  socket.send(JSON.stringify({id, method, params}));
  return new Promise((resolve, reject) => pending.set(id, {resolve, reject}));
}

async function evaluate(expression) {
  const result = await command('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

async function waitFor(expression, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`timed out waiting for ${expression}`);
}

async function backUntil(expression, limit = 16) {
  for (let step = 0; step < limit; step += 1) {
    if (await evaluate(expression)) return;
    await evaluate('history.back()');
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`browser Back never restored ${expression}`);
}

async function secondTabConnects(url) {
  const target = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(url)}`,
    {method: 'PUT'},
  ).then(response => response.json());
  const second = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    second.addEventListener('open', resolve, {once: true});
    second.addEventListener('error', reject, {once: true});
  });
  let secondId = 0;
  const secondPending = new Map();
  second.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    const request = secondPending.get(message.id);
    if (!request) return;
    secondPending.delete(message.id);
    if (message.error) request.reject(new Error(message.error.message));
    else request.resolve(message.result);
  });
  const call = (method, params = {}) => {
    const id = ++secondId;
    second.send(JSON.stringify({id, method, params}));
    return new Promise((resolve, reject) => secondPending.set(id, {resolve, reject}));
  };
  const deadline = Date.now() + 10000;
  try {
    while (Date.now() < deadline) {
      const result = await call('Runtime.evaluate', {
        expression: 'document.querySelector("#workspace")?.hidden === false',
        returnByValue: true,
      });
      if (result.result.value === true) return true;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return false;
  } finally {
    await call('Page.close');
    second.close();
  }
}

try {
  await command('Page.enable');
  await command('Runtime.enable');
  await command('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await command('Page.navigate', {url: `${origin}/overview`});
  await waitFor('document.readyState === "complete"');
  await waitFor('document.querySelector("#workspace")?.hidden === false');
  await waitFor('document.querySelector("#home-view")?.hidden === false');
  assert.deepEqual(
    await evaluate(`(() => {
      const selects = Array.from(document.querySelectorAll('select'));
      const triggers = Array.from(document.querySelectorAll('.viewer-dropdown .dropdown-trigger'));
      return [
        selects.length,
        triggers.length,
        selects.every(select => select.classList.contains('dropdown-native')),
        triggers.every(trigger => trigger.getAttribute('aria-haspopup') === 'listbox'),
      ];
    })()`),
    [7, 7, true, true],
    'every viewer select uses the shared styled dropdown control',
  );
  await evaluate(`document.querySelector('#local-access-error').hidden = false`);
  assert.equal(
    await evaluate(`(() => {
      const banner = document.querySelector('#local-access-error');
      const copy = banner.firstElementChild.getBoundingClientRect();
      const action = banner.querySelector('button').getBoundingClientRect();
      const style = getComputedStyle(banner);
      return style.position === 'fixed' && style.backdropFilter.includes('blur') && action.left > copy.right;
    })()`),
    true,
    'the local viewer error uses a translucent banner with its retry action on the right',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/local-error.png`, screenshot.data, 'base64');
  }
  await evaluate(`document.querySelector('#local-access-error').hidden = true`);
  assert.deepEqual(
    await evaluate(`(() => {
      const navbar = document.querySelector('.app-navbar').getBoundingClientRect();
      const attach = document.querySelector('#attach').getBoundingClientRect();
      return [
        document.querySelector('#instance-type').textContent,
        document.querySelector('#attach').closest('.navbar-actions') !== null,
        document.querySelector('#instance-type').closest('.app-footer') !== null,
        Math.abs(navbar.top) <= 1,
        navbar.right - attach.right <= 15,
        Math.abs(document.querySelector('.app-footer').getBoundingClientRect().bottom - window.innerHeight) <= 1,
        getComputedStyle(document.querySelector('.navbar-actions')).borderLeftWidth,
        navbar.bottom - attach.bottom >= 7,
        getComputedStyle(document.querySelector('.app-navbar'), '::after').backgroundColor,
      ];
    })()`),
    ['Local', true, true, true, true, true, '0px', true, 'rgb(17, 17, 17)'],
    'the top navigation keeps Attach Session clear of its full-width baseline while the footer identifies the local instance',
  );
  await evaluate(`(() => {
    document.querySelector('#environment-id').value = 'missing-environment-session';
    document.querySelector('#attach').click();
  })()`);
  await waitFor('document.querySelector(".toast[role=alert]")');
  assert.deepEqual(
    await evaluate(`(() => {
      const toast = document.querySelector('.toast[role=alert]');
      const region = toast.closest('.toast-region').getBoundingClientRect();
      const style = getComputedStyle(toast);
      return [
        region.top > document.querySelector('.app-navbar').getBoundingClientRect().bottom,
        region.right >= window.innerWidth - 21,
        toast.textContent.includes('HTTP 403: environment unavailable'),
        Boolean(toast.querySelector('[aria-label="Dismiss notification"]')),
        document.querySelector('#message') === null,
        style.backgroundColor.startsWith('rgba('),
        style.backdropFilter.includes('blur('),
      ];
    })()`),
    [true, true, true, true, true, true, true],
    'Attach failures appear in a dismissible translucent top-right toast above the footer',
  );
  await evaluate(`document.querySelector('.toast [aria-label="Dismiss notification"]').click()`);
  await waitFor('document.querySelector(".toast") === null');
  assert.equal(
    await evaluate('document.querySelector("#access")?.hidden'),
    true,
    'The local viewer never displays the credential form',
  );
  assert.equal(
    await evaluate('document.querySelector("#token")?.value'),
    '',
    'The local researcher credential is not retained in the hidden form',
  );
  await command('Page.reload');
  await waitFor('document.querySelector("#workspace")?.hidden === false');
  assert.equal(
    await secondTabConnects(`${origin}/overview`),
    true,
    'A second local viewer tab connects without sharing browser storage',
  );
  assert.equal(new URL(await evaluate('location.href')).pathname, '/overview', 'Overview has a stable URL');
  assert.deepEqual(
    await evaluate(`[
      Array.from(document.querySelectorAll('#global-destinations [data-destination]')).map(node => node.textContent),
      document.querySelector('#global-destinations')?.getAttribute('aria-label'),
      document.querySelector('[data-destination=overview]')?.getAttribute('aria-current'),
      document.querySelectorAll('#global-destinations [aria-current=page]').length,
      document.querySelector('#global-destinations svg') === null,
      document.querySelector('.home-heading .eyebrow')?.textContent,
      getComputedStyle(document.querySelector('.home-heading .eyebrow')).textTransform,
    ]`),
    [
      ['Overview', 'Experiments', 'Sessions', 'Trajectories'],
      'Global Navigation', 'page', 1, true, 'Overview', 'none',
    ],
    'the viewer exposes exactly four text-only global destinations and highlights the current one',
  );
  assert.deepEqual(
    await evaluate(`(() => {
      const bar = document.querySelector('#context-bar').getBoundingClientRect();
      return [
        document.querySelector('#overview-summary')?.hidden,
        document.querySelector('#home-list-content')?.hidden,
        document.querySelector('#context-nav')?.hidden,
        document.body.classList.contains('viewer-contextual'),
        Array.from(document.querySelectorAll('#overview-summary [data-overview-destination]')).map(node => node.dataset.overviewDestination),
        document.querySelector('#breadcrumb-trail')?.childElementCount,
        Math.round(bar.height),
      ];
    })()`),
    [false, true, true, false, ['experiments', 'sessions', 'trajectories'], 0, 38],
    'Overview is a collection landing page with no contextual drawer and a reserved breadcrumb bar',
  );
  await evaluate(`document.querySelector('[data-destination=experiments]').click()`);
  await waitFor(`location.pathname === '/experiments'`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#experiment-index')?.hidden,
      document.querySelectorAll('#experiment-index-list [data-experiment-id]').length,
      document.querySelector('[data-destination=experiments]')?.getAttribute('aria-current'),
      document.querySelectorAll('#global-destinations [aria-current=page]').length,
      document.querySelector('#context-nav')?.hidden,
      document.querySelector('.home-heading h1')?.textContent,
    ]`),
    [false, 1, 'page', 1, true, 'Experiments'],
    'Experiments is a full-width collection index without a contextual drawer',
  );
  await evaluate(`document.querySelector('[data-destination=trajectories]').click()`);
  await waitFor(`location.pathname === '/trajectories'`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#trajectory-index')?.hidden,
      document.querySelectorAll('#trajectory-index-list [data-trajectory-id]').length >= 3,
      [...new Set(Array.from(document.querySelectorAll('#trajectory-index-list [data-trajectory-origin]'))
        .map(node => node.dataset.trajectoryOrigin))].sort(),
      document.querySelector('[data-destination=trajectories]')?.getAttribute('aria-current'),
      document.querySelector('#context-nav')?.hidden,
    ]`),
    [false, true, ['imported', 'native'], 'page', true],
    'Trajectories lists native and imported evidence in one global index',
  );
  await evaluate(`document.querySelector('[data-destination=sessions]').click()`);
  await waitFor(`location.pathname === '/sessions'`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#home-list-content')?.hidden,
      document.querySelector('[data-destination=sessions]')?.getAttribute('aria-current'),
      document.querySelector('#context-nav')?.hidden,
    ]`),
    [false, 'page', true],
    'Sessions is the global environment-session index',
  );
  assert.equal(
    await evaluate('document.querySelectorAll("#home-session-list [data-session-id]").length'),
    3,
    'Home keeps standalone environment sessions as top-level rows',
  );
  assert.equal(
    await evaluate('document.querySelectorAll("#home-session-list [data-experiment-id]").length'),
    1,
    'Home groups related environment sessions under an experiment parent',
  );
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#experiment-count')?.textContent,
      document.querySelector('#scenario-count')?.textContent,
      document.querySelector('#session-count')?.textContent,
      document.querySelector('#activity-summary')?.hidden,
      document.querySelector('#visible-session-count')?.hidden,
    ]`),
    ['1', '2', '7', true, true],
    'Home summarizes the unfiltered experiment hierarchy without repeating an idle activity count',
  );
  assert.deepEqual(
    await evaluate(`(() => {
      const view = document.querySelector('#home-view');
      const table = document.querySelector('#home-session-list');
      const header = document.querySelector('.home-session-columns');
      const pagination = document.querySelector('#home-pagination');
      return [
        getComputedStyle(view).overflowY,
        getComputedStyle(table).overflowY,
        getComputedStyle(header).position,
        table.getBoundingClientRect().bottom <= pagination.getBoundingClientRect().top + 1,
        document.querySelector('#home-page-summary')?.textContent,
        document.querySelector('#home-page-current')?.textContent,
        document.querySelector('#home-page-total')?.textContent,
        document.querySelector('#home-page-previous svg') !== null,
        document.querySelector('#home-page-next svg') !== null,
        document.querySelector('#home-page-previous')?.textContent.trim(),
        document.querySelector('#home-page-next')?.textContent.trim(),
        getComputedStyle(document.querySelector('#home-page-previous')).borderLeftWidth,
        getComputedStyle(document.querySelector('#home-page-next')).backgroundColor,
        document.querySelector('#home-page-previous')?.disabled,
        document.querySelector('#home-page-next')?.disabled,
      ];
    })()`),
    ['hidden', 'auto', 'sticky', true, '1–4 of 4 entries', '1', '1', true, true, '', '', '0px', 'rgba(0, 0, 0, 0)', true, true],
    'Home contains scrolling inside the table and uses compact arrow pagination below it',
  );
  await evaluate(`(() => {
    const size = document.querySelector('#home-page-size');
    size.add(new Option('2 per page', '2'));
    size.value = '2';
    size.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`document.querySelector('#home-page-summary')?.textContent === '1–2 of 4 entries'`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelectorAll('#home-session-list [data-experiment-id]').length,
      document.querySelectorAll('#home-session-list .standalone-session').length,
      document.querySelector('#home-page-previous')?.disabled,
      document.querySelector('#home-page-next')?.disabled,
    ]`),
    [1, 1, true, false],
    'the first page keeps the experiment hierarchy intact and fills the remaining entry slot',
  );
  await evaluate(`document.querySelector('#home-session-list .standalone-session input').click()`);
  await waitFor(`document.querySelector('#home-selection-count')?.textContent === '1 session selected'`);
  await evaluate(`document.querySelector('#home-page-next').click()`);
  await waitFor(`document.querySelector('#home-page-summary')?.textContent === '3–4 of 4 entries'`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelectorAll('#home-session-list [data-experiment-id]').length,
      document.querySelectorAll('#home-session-list .standalone-session').length,
      document.querySelector('#home-page-previous')?.disabled,
      document.querySelector('#home-page-next')?.disabled,
    ]`),
    [0, 2, false, true],
    'Next advances through top-level entries without splitting an experiment',
  );
  await evaluate(`document.querySelector('#home-session-list .standalone-session input').click()`);
  await waitFor(`document.querySelector('#compare')?.textContent === 'Compare 2 Sessions'`);
  assert.equal(
    await evaluate(`document.querySelector('#home-list-content').hidden === false &&
      document.querySelector('#home-selection').getBoundingClientRect().top <
      document.querySelector('#home-session-list').getBoundingClientRect().top`),
    true,
    'selection persists across pages and keeps Compare above the scrolling table',
  );
  await evaluate(`document.querySelector('#clear-session-selection').click()`);
  await evaluate(`(() => {
    const size = document.querySelector('#home-page-size');
    size.value = '25';
    size.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`document.querySelector('#home-page-summary')?.textContent === '1–4 of 4 entries'`);
  await evaluate(`(() => {
    const trigger = document.querySelector('#session-status-trigger');
    trigger.focus(); trigger.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowDown', bubbles:true}));
  })()`);
  await waitFor('document.querySelector("#session-status-options")?.hidden === false');
  await waitFor('document.activeElement?.closest("#session-status-options") !== null');
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#session-status-trigger')?.getAttribute('aria-expanded'),
      document.querySelector('#session-status-options')?.getAttribute('role'),
      document.querySelectorAll('#session-status-options .dropdown-option').length,
      getComputedStyle(document.querySelector('#session-status-options')).position,
      getComputedStyle(document.querySelector('#session-status-options')).backgroundColor,
    ]`),
    ['true', 'listbox', 2, 'absolute', 'rgb(255, 255, 255)'],
    'the status dropdown opens as a styled, accessible listbox',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/home-dropdown.png`, screenshot.data, 'base64');
  }
  await evaluate(`document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))`);
  await waitFor('document.querySelector("#session-status-options")?.hidden === true');
  await evaluate(`document.querySelector('#session-sort-trigger').click()`);
  await waitFor('document.querySelector("#session-sort-options")?.hidden === false');
  assert.equal(
    await evaluate(`document.querySelectorAll('#session-sort-options .dropdown-option').length`),
    2,
    'the sort dropdown uses the same styled option menu',
  );
  await evaluate(`document.querySelector('#session-sort-trigger').click()`);
  await evaluate(`(() => {
    const button = document.querySelector('#refresh');
    window.__idleRefreshWidth = button.getBoundingClientRect().width;
    window.__refreshStarted = performance.now();
    button.click();
  })()`);
  assert.deepEqual(
    await evaluate(`(() => {
      const button = document.querySelector('#refresh');
      const label = button.querySelector('.refresh-label');
      return [
        button.disabled,
        button.getAttribute('aria-busy'),
        button.classList.contains('is-loading'),
        getComputedStyle(button).cursor,
        button.textContent,
        Math.abs(button.getBoundingClientRect().width - window.__idleRefreshWidth) <= 1,
        getComputedStyle(button).transform !== 'none',
        getComputedStyle(button).transitionProperty === 'transform',
        Boolean(label) && getComputedStyle(label).visibility === 'hidden',
      ];
    })()`),
    [true, 'true', true, 'default', 'Refresh', true, true, true, true],
    'Refresh replaces its visible label with a centered spinner without changing width or cursor',
  );
  await waitFor('document.querySelector("#refresh")?.textContent === "Refresh" && !document.querySelector("#refresh")?.disabled');
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#refresh')?.textContent,
      document.querySelector('#refresh')?.hasAttribute('aria-busy'),
      document.querySelector('#list-synced')?.textContent.startsWith('List synced at '),
      Boolean(document.querySelector('#list-synced')?.getAttribute('datetime')),
      performance.now() - window.__refreshStarted >= 450,
    ]`),
    ['Refresh', false, true, true, true],
    'Refresh remains visibly active before returning to idle with the latest complete sync time',
  );
  assert.deepEqual(
    await evaluate(`(() => {const button=document.querySelector('.experiment-disclosure'); const box=button.getBoundingClientRect(); return [button.textContent, button.getAttribute('aria-expanded'), Boolean(button.getAttribute('aria-controls')), box.width > 1, box.height > 1];})()`),
    ['View 4 sessions', 'false', true, true, true],
    'experiment disclosure visibly announces its environment-session count and accessible state',
  );
  await evaluate(`document.querySelector('.experiment-disclosure').click()`);
  await waitFor('document.querySelectorAll(".scenario-session[data-session-id]").length === 4');
  assert.deepEqual(
    await evaluate(`[
      location.pathname,
      document.querySelector('.experiment-name')?.tagName,
      document.querySelector('.experiment-name')?.getAttribute('href')?.startsWith('/experiments/'),
      document.querySelectorAll('.scenario-row[data-scenario-id]').length,
      document.querySelectorAll('.standalone-session[data-session-id]').length,
    ]`),
    ['/sessions', 'A', true, 0, 3],
    'expanding an experiment keeps the index URL while its name links to dedicated details',
  );
  assert.equal(
    await evaluate('document.querySelector(".experiment-disclosure").textContent'),
    'Hide sessions',
    'the disclosure expands environment sessions directly below the experiment',
  );
  assert.deepEqual(
    await evaluate(`Array.from(document.querySelector('.home-session-columns')?.children ?? []).map(node => node.textContent)`),
    ['', 'Name', 'Type', 'Status', 'Sessions', 'Turns'],
    'Home preserves the established row distinctions and aggregate count columns',
  );
  assert.equal(
    await evaluate(`getComputedStyle(document.querySelector('.home-session-columns > :last-child')).textAlign`),
    'center',
    'Turns aligns consistently with the other metadata columns',
  );
  assert.equal(
    await evaluate(`(() => {
      const table = document.querySelector('#home-session-list').getBoundingClientRect();
      const columns = [3, 4, 5, 6].map(index =>
        document.querySelector('.home-session-columns > :nth-child(' + index + ')').getBoundingClientRect());
      const centers = columns.map(column => (column.left + column.right) / 2);
      const gaps = centers.slice(1).map((center, index) => center - centers[index]);
      return columns[0].left < table.left + table.width * .75 &&
        Math.max(...gaps) - Math.min(...gaps) <= 2 && table.right - columns.at(-1).right >= 68;
    })()`),
    true,
    'the metadata columns are evenly spaced with breathing room at the table edge',
  );
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('.experiment-parent')?.children[2]?.textContent,
      document.querySelector('.scenario-session')?.children[2]?.textContent,
      document.querySelector('.standalone-session')?.children[2]?.textContent,
      document.querySelector('.experiment-parent')?.children[3]?.textContent,
      document.querySelector('.scenario-session')?.children[3]?.textContent,
      document.querySelector('.standalone-session')?.children[3]?.textContent,
      document.querySelector('.experiment-parent')?.children[4]?.textContent,
      document.querySelector('.scenario-session')?.children[4]?.textContent,
      document.querySelector('[data-turn-count="24"]')?.children[4]?.textContent,
      document.querySelector('.experiment-parent')?.children[5]?.textContent,
      document.querySelector('.scenario-session')?.children[5]?.textContent,
      document.querySelector('.scenario-session .home-session-cell small')?.textContent.includes('Agent'),
      document.querySelector('.scenario-session .home-session-cell small')?.textContent.includes('2 of 2 turns'),
      document.querySelector('.scenario-session .home-session-cell small')?.textContent.includes('Completed'),
    ]`),
    ['Experiment', 'Session', 'Session', 'Completed', 'Completed', 'Completed', '4', '1', '1', '8', '2', true, true, true],
    'the table keeps its established columns while session identity includes participants, target turns, and activity',
  );
  assert.equal(
    await evaluate(`document.querySelector('.experiment-children .session-name')?.textContent`),
    'Routine request · Trial 1',
    'environment-session rows combine the scenario name with their trial',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/home-hierarchy.png`, screenshot.data, 'base64');
  }
  await evaluate(`document.querySelector('.experiment-name').click()`);
  await waitFor(`location.pathname.startsWith('/experiments/')`);
  assert.deepEqual(
    await evaluate(`[
      /^\\/experiments\\/[^/]+$/.test(location.pathname),
      document.querySelector('.home-heading h1')?.textContent,
      Array.from(document.querySelectorAll('#context-nav [data-context-item]')).map(tab => tab.textContent),
      document.querySelector('#context-nav .context-nav-heading')?.textContent,
      document.querySelector('#context-nav')?.hidden,
      document.body.classList.contains('viewer-contextual'),
      document.querySelectorAll('#context-nav [aria-current=page]').length,
      document.querySelector('[data-context-item=overview]')?.getAttribute('aria-current'),
      document.querySelector('#experiment-overview')?.hidden,
      document.querySelector('#home-list-content')?.hidden,
      document.querySelector('[data-destination=experiments]')?.getAttribute('aria-current'),
      Array.from(document.querySelectorAll('#breadcrumb-trail a')).map(node => node.textContent),
      document.querySelector('#breadcrumb-trail')?.textContent.includes('Support response evaluation'),
      getComputedStyle(document.querySelector('#home-view')).backgroundColor,
    ]`),
    [
      true, 'Support response evaluation',
      ['Overview', 'Scenarios', 'Sessions', 'Trajectories', 'Configuration'],
      'Experiment', false, true, 1, 'page', false, true, 'page', ['Experiments'], false,
      'rgb(255, 255, 255)',
    ],
    'an experiment opens one contextual left navigation and a breadcrumb that stops at its parent',
  );
  assert.equal(
    await evaluate(`(() => {
      const nav = document.querySelector('#context-nav').getBoundingClientRect();
      const content = document.querySelector('#home-view').getBoundingClientRect();
      return document.querySelectorAll('.context-nav, .session-drawer').length === 1 &&
        nav.right <= content.left + 1;
    })()`),
    true,
    'the contextual drawer never stacks with a second permanent drawer',
  );
  await evaluate(`document.querySelector('[data-context-item=configuration]').click()`);
  await waitFor(`location.pathname.endsWith('/configuration')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#experiment-configuration')?.hidden,
      document.querySelector('#experiment-frozen-summary')?.textContent.includes('synthetic-protocol@1'),
      document.querySelector('[data-context-item=configuration]')?.getAttribute('aria-current'),
      Math.round(document.querySelector('#context-bar').getBoundingClientRect().height),
    ]`),
    [false, true, 'page', 38],
    'Configuration holds the frozen experiment and leaves the breadcrumb height unchanged',
  );
  await evaluate(`document.querySelector('[data-context-item=trajectories]').click()`);
  await waitFor(`location.pathname.endsWith('/trajectories')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#experiment-trajectories')?.hidden,
      document.querySelectorAll('#experiment-trajectory-list [data-trajectory-id]').length,
      Math.round(document.querySelector('#context-bar').getBoundingClientRect().height),
    ]`),
    [false, 4, 38],
    'an experiment lists the trajectories its sessions recorded',
  );
  await evaluate(`document.querySelector('[data-context-item=scenarios]').click()`);
  await waitFor(`location.pathname.endsWith('/scenarios')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelectorAll('.experiment-scenario-row').length,
      document.querySelector('.experiment-scenario-row')?.textContent.includes('Routine request'),
      document.querySelector('[data-context-item=scenarios]')?.getAttribute('aria-current'),
    ]`),
    [2, true, 'page'],
    'meaningful scenario snapshots have a dedicated experiment page',
  );
  await evaluate(`document.querySelector('.experiment-scenario-row').click()`);
  await waitFor(`location.pathname.includes('/scenarios/easy-case')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#scenario-detail-title')?.textContent,
      document.querySelector('#scenario-detail')?.textContent.includes('Input'),
      document.querySelector('#scenario-detail')?.textContent.includes('Metadata'),
      document.querySelector('#scenario-view-sessions')?.textContent,
      document.querySelector('.home-heading h1')?.textContent,
      Array.from(document.querySelectorAll('#context-nav [data-context-item]')).map(tab => tab.textContent),
      document.querySelector('#context-nav .context-nav-heading')?.textContent,
      Array.from(document.querySelectorAll('#breadcrumb-trail a')).map(node => node.textContent),
      document.querySelector('[data-destination=experiments]')?.getAttribute('aria-current'),
      Math.round(document.querySelector('#context-bar').getBoundingClientRect().height),
    ]`),
    [
      'Routine request', true, true, 'View Sessions', 'Routine request',
      ['Overview', 'Sessions', 'Trajectories'], 'Scenario',
      ['Experiments', 'Support response evaluation'], 'page', 38,
    ],
    'a scenario opens its own contextual navigation, keeps Experiments highlighted, and shows ownership ancestry',
  );
  await evaluate(`document.querySelector('#scenario-view-sessions').click()`);
  await waitFor(`location.pathname.endsWith('/scenarios/easy-case/sessions')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('.home-list-heading h2')?.textContent,
      document.querySelectorAll('.scenario-session[data-session-id]').length,
      document.querySelector('#session-search')?.value,
      document.querySelector('[data-context-item=sessions]')?.getAttribute('aria-current'),
    ]`),
    ['Scenario Sessions', 2, 'easy-case', 'page'],
    'View Sessions opens the session list filtered to the selected scenario',
  );
  await evaluate(`document.querySelector('.scenario-session .session-name').click()`);
  await waitFor(`location.pathname.startsWith('/sessions/')`);
  const nestedSessionId = await evaluate('document.querySelector("#environment-identity")?.textContent');
  assert.deepEqual(
    await evaluate(`[
      /^\\/sessions\\/[^/]+$/.test(location.pathname),
      Array.from(document.querySelectorAll('#breadcrumb-trail a')).map(node => node.textContent),
      document.querySelector('#breadcrumb-trail a:nth-of-type(2)')?.getAttribute('href')?.startsWith('/experiments/'),
      document.querySelector('#breadcrumb-trail')?.textContent.includes('Routine request · Trial 1'),
      document.querySelector('#context-title')?.textContent,
      document.querySelector('[data-destination=experiments]')?.getAttribute('aria-current'),
      Array.from(document.querySelectorAll('#context-nav [data-context-item]')).map(tab => tab.textContent),
      Math.round(document.querySelector('#context-bar').getBoundingClientRect().height),
    ]`),
    [
      true, ['Experiments', 'Support response evaluation', 'Routine request'], true, false,
      'Routine request · Trial 1', 'page',
      ['Overview', 'Turns', 'Progression', 'Trajectory', 'Configuration'], 38,
    ],
    'a Session shows ownership ancestry that stops at its Scenario while the heading owns its name',
  );
  await evaluate(`document.querySelectorAll('#breadcrumb-trail a')[1].click()`);
  await waitFor(`/^\\/experiments\\/[^/]+$/.test(location.pathname)`);
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/experiment.png`, screenshot.data, 'base64');
  }
  await evaluate(`document.querySelector('[data-destination=sessions]').click()`);
  await waitFor(`location.pathname === '/sessions'`);
  await evaluate(`document.querySelector('.experiment-parent > input[type=checkbox]').click()`);
  await waitFor('document.querySelector("#compare")?.textContent === "Compare 4 Sessions"');
  assert.equal(
    await evaluate(`(() => {
      const view = document.querySelector('#home-view');
      const selection = document.querySelector('#home-selection');
      const compare = document.querySelector('#compare');
      const table = document.querySelector('#home-session-list');
      table.scrollTop = table.scrollHeight;
      return table.scrollHeight > table.clientHeight && view.scrollTop === 0 &&
        compare.getBoundingClientRect().bottom <= selection.getBoundingClientRect().bottom &&
        selection.getBoundingClientRect().top < table.getBoundingClientRect().top;
    })()`),
    true,
    'scrolling a long table keeps the selection and Compare controls visible above it',
  );
  assert.equal(
    await evaluate(`document.querySelector('.experiment-parent > input').checked &&
      Array.from(document.querySelectorAll('.scenario-session > input')).every(input => input.checked)`),
    true,
    'selecting an experiment selects all of its child sessions',
  );
  await evaluate(`document.querySelector('.experiment-parent > input[type=checkbox]').click()`);
  await waitFor('document.querySelector("#home-selection")?.hidden === true');
  await evaluate(`document.querySelector('.experiment-disclosure').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowLeft', bubbles:true}))`);
  await waitFor('document.querySelectorAll(".scenario-session[data-session-id]").length === 0');
  await evaluate(`document.querySelector('.experiment-parent > input[type=checkbox]').click()`);
  await waitFor('document.querySelectorAll(".scenario-session[data-session-id]").length === 4');
  assert.equal(
    await evaluate(`document.querySelector('.experiment-parent > input').checked &&
      Array.from(document.querySelectorAll('.scenario-session > input')).every(input => input.checked)`),
    true,
    'selecting a collapsed experiment reveals and selects every environment session',
  );
  await evaluate(`document.querySelector('.experiment-parent > input[type=checkbox]').click()`);
  await waitFor('document.querySelector("#home-selection")?.hidden === true');
  await evaluate(`document.querySelector('.experiment-parent').click()`);
  await waitFor('document.querySelectorAll(".scenario-session[data-session-id]").length === 0');
  assert.equal(
    await evaluate('document.querySelector(".session-drawer") === null'),
    true,
    'Home replaces the persistent environment-session drawer',
  );
  assert.equal(
    await evaluate('document.querySelector("#home-view")?.textContent.includes("Highlights")'),
    false,
    'Home does not claim that recent activity is important without ranking rules',
  );
  await evaluate(`(() => {
    const search = document.querySelector('#session-search');
    search.value = 'does-not-exist'; search.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor('document.querySelectorAll("#home-session-list [data-session-id]").length === 0');
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#session-count')?.textContent,
      document.querySelector('#visible-session-count')?.textContent,
      document.querySelector('#visible-session-count')?.hidden,
    ]`),
    ['7', '0 visible', false],
    'filtering preserves inventory totals and reports the visible result count separately',
  );
  await evaluate(`(() => {
    const search = document.querySelector('#session-search');
    search.value = ''; search.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor('document.querySelectorAll("#home-session-list [data-session-id]").length === 3');
  await evaluate(`(() => {
    const search = document.querySelector('#session-search');
    search.value = 'hard-case'; search.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor('document.querySelectorAll(".scenario-session[data-session-id]").length === 2');
  assert.equal(
    await evaluate(`document.querySelector('.experiment-parent')?.textContent.includes('2 matching of 4 sessions')`),
    true,
    'filtering through a child reveals that match without changing the complete session count',
  );
  await evaluate(`(() => {const search=document.querySelector('#session-search'); search.value=''; search.dispatchEvent(new Event('input', {bubbles:true}));})()`);
  assert.deepEqual(
    await evaluate(`Array.from(document.querySelector('#session-sort')?.options ?? []).map(option => option.textContent)`),
    ['Most recent', 'Oldest first'],
    'Home sorting does not reintroduce turn progress as an index concept',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/home.png`, screenshot.data, 'base64');
  }

  await evaluate(`document.querySelector('.standalone-session input[type=checkbox]').click()`);
  await waitFor('document.querySelector("#compare")?.textContent === "Select Another Session"');
  assert.equal(
    await evaluate(`(() => {
      const selection = document.querySelector('#home-selection').getBoundingClientRect();
      const action = document.querySelector('#compare').getBoundingClientRect();
      return action.height <= 36 && action.height < selection.height;
    })()`),
    true,
    'the selected-session action stays compact inside the selection bar',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/home-selected.png`, screenshot.data, 'base64');
  }
  await evaluate(`document.querySelector('#home-session-list [data-turn-count="24"] .session-name').click()`);
  await waitFor('document.querySelector("#context-title")?.textContent === "12 Participants"');
  const openedSessionId = await evaluate('document.querySelector("#environment-identity")?.textContent');
  assert.equal(
    new URL(await evaluate('location.href')).pathname,
    `/sessions/${openedSessionId}`,
    'opening an environment session updates the URL to its canonical detail root',
  );

  assert.equal(
    await evaluate('document.querySelector("#context-title")?.textContent'),
    '12 Participants',
    'large participant rosters use a compact session title',
  );
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#session-shell .page-context .eyebrow')?.textContent,
      getComputedStyle(document.querySelector('#session-shell .page-context .eyebrow')).textTransform,
      getComputedStyle(document.querySelector('#session-shell .page-context .eyebrow')).fontSize,
      getComputedStyle(document.querySelector('.home-heading .eyebrow')).fontSize,
    ]`),
    ['Session', 'none', '12px', '12px'],
    'page eyebrows use consistent, readable mixed-case typography',
  );
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('[data-destination=overview] span')?.textContent,
      getComputedStyle(document.querySelector('.navbar-actions')).display,
      Array.from(document.querySelectorAll('#breadcrumb-trail > *')).map(node => node.textContent).join(''),
      document.querySelector('#context-nav')?.previousElementSibling === null,
      document.querySelector('#global-destinations svg') === null,
      getComputedStyle(document.querySelector('[data-destination=overview]')).fontSize,
      getComputedStyle(document.querySelector('.breadcrumb-link')).fontSize,
    ]`),
    ['Overview', 'flex', 'Sessions', true, true, '12px', '12px'],
    'the navbar and contextual navigation change when a user enters an environment session',
  );
  assert.equal(
    await evaluate('parseFloat(getComputedStyle(document.querySelector(".page-context")).paddingLeft) >= 40'),
    true,
    'the selected environment session uses the same generous side gutter as Home',
  );
  assert.deepEqual(
    await evaluate(`Array.from(document.querySelectorAll('#context-nav [data-context-item]')).map(node => node.textContent)`),
    ['Overview', 'Turns', 'Progression', 'Trajectory', 'Configuration'],
    'a selected environment session separates summary, inspection, progression, trajectory, and configuration',
  );
  assert.equal(
    await evaluate('document.querySelector("[data-context-item=overview]")?.getAttribute("aria-current")'),
    'page',
    'opening a session starts at its session-level overview',
  );
  await waitFor('document.querySelectorAll("#evaluation-metrics .evaluation-metric:not(.evaluation-metric-heading)").length === 3');
  assert.equal(
    await evaluate(`document.querySelector('#session-configuration')?.textContent.includes('Max Turns') &&
      document.querySelector('#session-overview')?.textContent.includes('Scores and Findings') &&
      document.querySelector('#reports')?.textContent.includes('Synthetic total') &&
      document.querySelector('#reports')?.textContent.includes('Latest') &&
      document.querySelector('#reports')?.textContent.includes('Change') &&
      document.querySelector('#reports')?.textContent.includes('Malformed') &&
      document.querySelector('#reports')?.textContent.includes('Blocked')`),
    true,
    'Overview summarizes the metrics and findings returned by the environment session',
  );
  assert.equal(
    await evaluate(`document.querySelectorAll('#reports .report').length === 0 &&
      document.querySelector('#reports pre') === null &&
      !document.querySelector('#reports')?.textContent.includes('Score report revision')`),
    true,
    'Overview does not render the report log or raw JSON',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/overview.png`, screenshot.data, 'base64');
  }

  await evaluate(`(() => {
    const viewerFetch = window.fetch.bind(window);
    window.__viewerRequests = [];
    window.fetch = (...args) => {
      const input = args[0] instanceof Request ? args[0].url : String(args[0]);
      window.__viewerRequests.push(new URL(input, location.origin).pathname);
      return viewerFetch(...args);
    };
  })()`);
  await evaluate('document.querySelector("[data-context-item=turns]").click()');
  await waitFor('location.pathname.endsWith("/turns")');
  await waitFor(`window.__viewerRequests.includes('/v1/sessions/${openedSessionId}') &&
    window.__viewerRequests.includes('/v1/sessions/${openedSessionId}/scores')`);
  assert.equal(
    await evaluate(`window.__viewerRequests.includes('/v1/sessions/${openedSessionId}') &&
      window.__viewerRequests.includes('/v1/sessions/${openedSessionId}/evidence') &&
      window.__viewerRequests.includes('/v1/sessions/${openedSessionId}/scores')`),
    true,
    'entering a session section refreshes its metadata, evidence, and reports',
  );
  await waitFor('document.querySelectorAll("#turn-list [data-turn]").length === 24');
  assert.equal(
    await evaluate('document.querySelector("#turn-list").scrollHeight > document.querySelector("#turn-list").clientHeight'),
    true,
    'Turn History scrolls when a session has many turns',
  );
  assert.equal(
    await evaluate(`Math.abs(document.querySelector('.inspect-grid').getBoundingClientRect().bottom -
      document.querySelector('.app-footer').getBoundingClientRect().top) <= 1`),
    true,
    'Turn History and the inspect workspace fill the viewport',
  );
  await waitFor('document.querySelectorAll("#participant-evidence-list [data-participant]").length === 12');
  assert.equal(
    await evaluate(`document.querySelector('.turn-detail').scrollHeight >
      document.querySelector('.turn-detail').clientHeight`),
    true,
    'Turn Details scrolls when a turn has extensive participant evidence',
  );
  assert.equal(
    await evaluate('document.documentElement.scrollHeight === window.innerHeight'),
    true,
    'Inspect keeps scrolling inside its workspace instead of scrolling the whole page',
  );
  assert.equal(
    await evaluate('document.querySelector("#selected-evidence pre") === null'),
    true,
    'Selected Evidence explains events without displaying raw JSON',
  );
  assert.equal(
    await evaluate('document.querySelector(".evidence-inspector") === null'),
    true,
    'Selected Evidence no longer occupies a narrow side drawer',
  );

  assert.equal(
    await evaluate('document.querySelector(".turn-detail")?.textContent.includes("Frozen Experiment")'),
    false,
    'turn details do not present session-level configuration as turn-specific',
  );
  assert.equal(
    await evaluate(`Math.abs(document.querySelector('.sidebar-brand').getBoundingClientRect().bottom -
      document.querySelector('.app-navbar').getBoundingClientRect().bottom) <= 1`),
    true,
    'the EnvironmentHarness brand divider aligns with the viewer navbar',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    await evaluate(`(() => {
      const detail = document.querySelector('.turn-detail');
      detail.scrollTop = detail.scrollHeight;
    })()`);
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/inspect.png`, screenshot.data, 'base64');
  }

  await evaluate('document.querySelector("#reports-progression").click()');
  await waitFor('location.pathname.endsWith("/progression")');
  await waitFor(`Array.from(document.querySelectorAll('#turn-series-select option'))
    .some(option => option.textContent.includes('Synthetic total'))`);
  await evaluate(`document.querySelector('#turn-series-select-trigger').click()`);
  await waitFor('document.querySelector("#turn-series-select-options")?.hidden === false');
  assert.deepEqual(
    await evaluate(`(() => {
      const select = document.querySelector('#turn-series-select');
      const trigger = document.querySelector('#turn-series-select-trigger');
      const menu = document.querySelector('#turn-series-select-options');
      return [
        select.closest('.viewer-dropdown') !== null,
        trigger.getAttribute('aria-expanded'),
        menu.getAttribute('role'),
        getComputedStyle(menu).position,
        menu.textContent.includes('Participant Reward'),
      ];
    })()`),
    [true, 'true', 'listbox', 'absolute', true],
    'the Progression Series control opens with the shared styled dropdown',
  );
  await evaluate(`document.querySelector('#turn-series-select-trigger').click()`);
  assert.equal(
    await evaluate(`['Participant Reward', 'Blocked Attempts', 'Recorded Findings · Malformed', 'synthetic.total · Total']
      .every(label => Array.from(document.querySelectorAll('#turn-series-select option')).some(option => option.textContent === label))`),
    true,
    'the synthetic example exposes rewards, activity, findings and environment signals',
  );
  assert.equal(
    await evaluate('document.querySelector("#turn-series-select")?.value'),
    'metric:synthetic-showcase:1:synthetic_total',
    'Progression defaults to the versioned score series recorded by the example',
  );
  await waitFor('document.querySelectorAll("#turn-series-chart [data-series-point]").length === 24');
  assert.equal(
    await evaluate(`(() => {
      const chart = document.querySelector('#turn-series-chart');
      const points = Array.from(chart.querySelectorAll('[data-series-point]'));
      const rightmost = Math.max(...points.map(point => Number(point.getAttribute('x')) + Number(point.getAttribute('width'))));
      return Math.abs(chart.viewBox.baseVal.width - chart.getBoundingClientRect().width) <= 1 &&
        rightmost > chart.viewBox.baseVal.width * .9;
    })()`),
    true,
    'the progression graph fills the available chart width',
  );
  assert.equal(
    await evaluate(`document.querySelector('#turn-series-markers')?.textContent.includes('Checkpoint') &&
      document.querySelector('#turn-series-markers')?.textContent.includes('Recorded Findings · Malformed')`),
    true,
    'the example places checkpoints and attributed findings on the progression',
  );
  assert.equal(
    await evaluate('document.querySelector("#turn-series-chart circle") === null'),
    true,
    'the series chart avoids decorative circular point markers',
  );
  await evaluate(`(() => {
    const start = document.querySelector('#compare-turn-start');
    const end = document.querySelector('#compare-turn-end');
    start.value = '5'; start.dispatchEvent(new Event('input', {bubbles: true}));
    end.value = '8'; end.dispatchEvent(new Event('input', {bubbles: true}));
  })()`);
  await waitFor('document.querySelectorAll("#turn-series-chart [data-series-point]").length === 4');
  assert.equal(
    await evaluate('document.querySelector("#turn-compare-summary")?.textContent.includes("4 turns in view")'),
    true,
    'Progression follows the selected turn range inside one environment session',
  );
  assert.equal(
    await evaluate(`document.querySelector('#turn-range-start-value')?.textContent === 'Turn 5' &&
      document.querySelector('#turn-range-end-value')?.textContent === 'Turn 8'`),
    true,
    'the range names its starting and ending turns',
  );
  await waitFor(`document.querySelector('#turn-difference')?.textContent.includes('Change from Turn 5 to Turn 8')`);
  assert.equal(
    await evaluate(`document.querySelector('#turn-difference')?.textContent.includes('Turn 5 compared with Turn 8') &&
      document.querySelector('#turn-difference')?.textContent.includes('Synthetic total') &&
      document.querySelector('#turn-difference')?.textContent.includes('Change')`),
    false,
    'Progression describes the selected change without presenting the whole view as a comparison',
  );
  assert.equal(
    await evaluate(`document.querySelector('#turn-difference')?.textContent.includes('Synthetic total') &&
      document.querySelector('#turn-difference')?.textContent.includes('Change')`),
    true,
    'the chart range resolves to a grounded comparison of its boundary turns',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/progression.png`, screenshot.data, 'base64');
  }
  const selectionRect = await evaluate(`(() => {
    const rect = document.querySelector('#turn-range-selection').getBoundingClientRect();
    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
  })()`);
  await command('Input.dispatchMouseEvent', {type: 'mousePressed', x: selectionRect.x, y: selectionRect.y, button: 'left', clickCount: 1});
  assert.equal(
    await evaluate(`document.querySelector('#turn-range-selection').classList.contains('is-dragging')`),
    true,
    'the selected range visibly indicates active dragging',
  );
  await command('Input.dispatchMouseEvent', {type: 'mouseMoved', x: selectionRect.x + 20, y: selectionRect.y, button: 'left'});
  await command('Input.dispatchMouseEvent', {type: 'mouseReleased', x: selectionRect.x + 20, y: selectionRect.y, button: 'left', clickCount: 1});
  await waitFor(`document.querySelector('#compare-turn-start').value === '6' &&
    document.querySelector('#compare-turn-end').value === '9'`);
  assert.equal(
    await evaluate(`document.querySelector('#turn-range-selection').classList.contains('is-dragging')`),
    false,
    'the range responds to a short drag and clears its active state on release',
  );
  await command('Input.dispatchMouseEvent', {
    type: 'mouseWheel', x: selectionRect.x, y: selectionRect.y, deltaX: 0, deltaY: 100,
  });
  await waitFor(`document.querySelector('#compare-turn-start').value === '7' &&
    document.querySelector('#compare-turn-end').value === '10'`);
  assert.equal(
    await evaluate(`document.querySelector('#turn-range-selection').classList.contains('is-scrolling')`),
    true,
    'scrolling over the range moves it sideways and shows active feedback',
  );

  await evaluate('document.querySelector("[data-context-item=overview]").click()');
  await waitFor('location.pathname.endsWith("' + openedSessionId + '")');
  assert.equal(
    await evaluate(`document.querySelectorAll('#report-history .report-record').length === 24 &&
      document.querySelectorAll('#report-history .report-line-card').length === 3 &&
      document.querySelectorAll('#report-history .report-latest-metrics .metric').length === 3 &&
      Array.from(document.querySelectorAll('#report-history .report-record')).every(node => !node.open) &&
      Array.from(document.querySelectorAll('#report-history .report-record-body > details > summary')).every(node => node.textContent === 'Raw')`),
    true,
    'session Overview summarizes latest values and progression while preserving expandable versioned records',
  );
  assert.deepEqual(
    await evaluate(`(() => {
      const record = document.querySelector('#report-history .report-record');
      const summary = record.querySelector('.report-record-summary');
      const closed = [getComputedStyle(summary).position, getComputedStyle(summary, '::after').content,
        getComputedStyle(summary, '::after').left];
      record.open = true;
      const opened = getComputedStyle(summary, '::after').content;
      record.open = false;
      return [...closed, opened];
    })()`),
    ['relative', '"+"', '10px', '"−"'],
    'each report revision anchors its plus and minus disclosure state to its own row',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/reports.png`, screenshot.data, 'base64');
  }

  await evaluate(`(() => { window.__viewerRequests = []; document.querySelector('[data-destination=sessions]').click(); })()`);
  await waitFor('document.querySelector("#home-view")?.hidden === false');
  await waitFor(`window.__viewerRequests.includes('/v1/sessions') &&
    window.__viewerRequests.includes('/v1/activity/hierarchy')`);
  assert.equal(new URL(await evaluate('location.href')).pathname, '/sessions', 'Sessions updates the URL');
  assert.deepEqual(
    await evaluate(`[new URL(location.href).searchParams.getAll('environment').length,
      document.querySelector('#home-selection').hidden,
      document.querySelectorAll('#home-session-list input[type=checkbox]:checked').length,
      document.querySelector('#context-nav')?.hidden,
      document.querySelector('#breadcrumb-trail')?.childElementCount]`),
    [0, true, 0, true, 0],
    'returning to the Sessions destination clears the selection, the drawer, and the breadcrumb trail',
  );
  assert.equal(
    await evaluate(`window.__viewerRequests.includes('/v1/sessions') &&
      window.__viewerRequests.includes('/v1/activity/hierarchy')`),
    true,
    'entering the Sessions destination refreshes the catalog and activity snapshot',
  );
  await backUntil(`location.pathname === '/sessions' &&
    new URL(location.href).searchParams.getAll('environment').length === 1`);
  await waitFor(`location.pathname === '/sessions' &&
    new URL(location.href).searchParams.getAll('environment').length === 1 &&
    document.querySelectorAll('#home-session-list input[type=checkbox]:checked').length === 1`);
  assert.equal(
    await evaluate(`document.querySelector('#home-selection-count')?.textContent`),
    '1 session selected',
    'browser Back restores the selection encoded in the earlier Sessions history entry',
  );
  await evaluate(`document.querySelector('#clear-session-selection').click()`);
  await waitFor(`document.querySelector('#home-selection')?.hidden === true`);
  assert.equal(
    await evaluate('getComputedStyle(document.querySelector(".session-status"), "::before").content'),
    'none',
    'session status uses text without a decorative line marker',
  );
  await evaluate(`(() => {
    const checks = document.querySelectorAll('#home-session-list .standalone-session input[type=checkbox]');
    checks[0].click(); checks[1].click();
  })()`);
  await waitFor('document.querySelector("#compare")?.textContent === "Compare 2 Sessions"');
  await evaluate('document.querySelector("#compare").click()');
  await waitFor('document.querySelector("#compare-sessions-view")?.hidden === false');
  assert.equal(new URL(await evaluate('location.href')).pathname, '/comparisons', 'session comparison has a stable URL');
  await waitFor('document.querySelectorAll("#compare-results .comparison-card").length === 2');
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#compare-session-list').hidden,
      document.querySelector('#run-comparison').hidden,
      document.querySelectorAll('#compare-results .comparison-card').length,
    ]`),
    [true, true, 2],
    'the Home selection runs immediately without showing another session picker or action',
  );
  await waitFor('document.querySelectorAll("#compare-results .comparison-metric-button").length === 3');
  assert.deepEqual(
    await evaluate(`[
      document.querySelectorAll('#compare-results .comparison-dot-marker').length,
      document.querySelectorAll('#compare-results .comparison-line-card').length,
      document.querySelectorAll('#compare-results .comparison-line').length,
      document.querySelector('#compare-results .comparison-metric-button[aria-pressed="true"]')?.textContent,
    ]`),
    [2, 1, 2, 'Synthetic Total'],
    'comparison renders final points and one shared-axis progression panel for the selected sessions',
  );
  assert.deepEqual(
    await evaluate(`(() => {
      const cards = Array.from(document.querySelectorAll('#compare-results .comparison-report-card'));
      return [cards.length,
        cards.map(card => card.querySelectorAll('.comparison-report-facts > div').length),
        cards.filter(card => card.querySelector('.comparison-report-aggregate strong')?.textContent).length,
        cards.filter(card => !card.querySelector('.comparison-report-details')?.open).length,
        document.querySelectorAll('#compare-results .comparison-report-context > div').length];
    })()`),
    [6, [6, 6, 6, 6, 6, 6], 6, 6, 2],
    'comparison reports use structured aggregates, coverage facts, and compact disclosures',
  );
  assert.equal(
    await evaluate(`(() => {
      const labels = Array.from(document.querySelectorAll('#compare-results .comparison-line-legend span')).map(node => node.textContent);
      const turnCounts = labels.map(label => label.match(/(\\d+) turns$/)?.[1]);
      const cardCounts = Array.from(document.querySelectorAll('#compare-results .comparison-card .comparison-session-facts')).map(facts =>
        Array.from(facts.querySelectorAll('div')).find(fact => fact.querySelector('dt')?.textContent === 'Total Turns')?.querySelector('dd')?.textContent);
      return labels.length === 2 && turnCounts.every(Boolean) && new Set(turnCounts).size === 2 &&
        JSON.stringify([...turnCounts].sort()) === JSON.stringify(cardCounts.sort()) &&
        document.querySelector('.comparison-length-note')?.textContent.includes('shorter sessions end at their actual final turn');
    })()`),
    true,
    'cards and charts expose exact unequal environment-session lengths and explain the shared axis',
  );
  assert.deepEqual(
    await evaluate(`[
      document.querySelectorAll('#compare-results .comparison-turn-range .turn-range-track').length,
      document.querySelectorAll('#compare-results .comparison-turn-range .turn-range-track input[type="range"]').length,
      document.querySelectorAll('#compare-results .comparison-turn-range .turn-range-selection').length,
      document.querySelectorAll('#compare-results .comparison-turn-range .turn-range-values').length,
      getComputedStyle(document.querySelector('#compare-results .comparison-progression-grid')).justifyContent,
    ]`),
    [1, 2, 1, 1, 'center'],
    'comparison reuses the Progression turn-window structure and centers compact charts',
  );
  await evaluate(`(() => {
    const start = document.querySelector('[aria-label="Start turn"]');
    const end = document.querySelector('[aria-label="End turn"]');
    start.value = '5'; end.value = '10';
    start.dispatchEvent(new Event('change', {bubbles: true}));
  })()`);
  await waitFor(`Array.from(document.querySelectorAll('#compare-results .comparison-turn-range .turn-range-values dd')).map(node => node.textContent).join() === 'Turn 5,Turn 10' &&
    !document.querySelector('#compare-results .comparison-visualization')?.hasAttribute('aria-busy')`);
  assert.equal(
    await evaluate(`(() => {
      const labels = Array.from(document.querySelectorAll('#compare-results .comparison-axis-label')).map(node => node.textContent);
      return labels.includes('T5') && labels.includes('T10');
    })()`),
    true,
    'turn-range controls request and render a bounded server-side window',
  );
  await evaluate(`document.querySelector('[data-metric="cumulative-reward"]').click()`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('[data-metric="cumulative-reward"]')?.getAttribute('aria-pressed'),
      document.querySelectorAll('#compare-results .comparison-point').length > 2,
      document.querySelector('#compare-results .comparison-chart-heading')?.textContent.includes('Cumulative Reward'),
    ]`),
    ['true', true, true],
    'comparison switches to cumulative reward without another request',
  );
  assert.deepEqual(
    await evaluate(`(() => {
      const parameters = new URL(location.href).searchParams;
      return [parameters.getAll('environment').length, parameters.get('metric'),
        parameters.get('start_turn'), parameters.get('end_turn')];
    })()`),
    [2, 'cumulative-reward', '5', '10'],
    'the comparison URL records selected environment sessions, metric, and turn window',
  );
  await command('Page.reload');
  await waitFor('document.querySelector("#workspace")?.hidden === false');
  await waitFor(`document.querySelector('#compare-sessions-view')?.hidden === false &&
    document.querySelectorAll('#compare-results .comparison-card').length === 2 &&
    document.querySelector('[data-metric="cumulative-reward"]')?.getAttribute('aria-pressed') === 'true' &&
    Array.from(document.querySelectorAll('.comparison-turn-range .turn-range-values dd')).map(node => node.textContent).join() === 'Turn 5,Turn 10'`);
  assert.equal(
    await evaluate(`document.querySelector('#compare-session-list').hidden &&
      document.querySelector('#run-comparison').hidden`),
    true,
    'refresh restores the comparison directly without reopening the picker',
  );
  await evaluate(`document.querySelector('#refresh').click()`);
  await waitFor(`!document.querySelector('#refresh').hasAttribute('aria-busy') &&
    document.querySelector('#compare-sessions-view')?.hidden === false &&
    document.querySelectorAll('#compare-results .comparison-card').length === 2 &&
    document.querySelector('[data-metric="cumulative-reward"]')?.getAttribute('aria-pressed') === 'true' &&
    Array.from(document.querySelectorAll('.comparison-turn-range .turn-range-values dd')).map(node => node.textContent).join() === 'Turn 5,Turn 10'`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('[data-destination=sessions]')?.getAttribute('aria-current'),
      document.querySelectorAll('#global-destinations [aria-current=page]').length,
      Array.from(document.querySelectorAll('#breadcrumb-trail a')).map(node => node.textContent),
      document.querySelector('#context-nav')?.hidden,
    ]`),
    ['page', 1, ['Sessions'], true],
    'a comparison highlights Sessions rather than introducing its own global destination',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/compare.png`, screenshot.data, 'base64');
  }
  await evaluate('document.querySelector("[data-destination=sessions]").click()');
  await evaluate('history.back()');
  await waitFor('document.querySelector("#compare-sessions-view")?.hidden === false');
  await evaluate('history.back()');
  await waitFor('document.querySelector("#home-view")?.hidden === false');
  await command('Page.navigate', {url: `${origin}/trajectories/${encodeURIComponent(trajectoryId)}`});
  await waitFor('document.querySelector("#trajectory-shell")?.hidden === false');
  await waitFor('document.querySelectorAll("#context-nav [data-context-item]").length === 4');
  assert.deepEqual(
    await evaluate(`[
      Array.from(document.querySelectorAll('#context-nav [data-context-item]')).map(node => node.textContent),
      document.querySelector('#context-nav .context-nav-heading')?.textContent,
      Array.from(document.querySelectorAll('#breadcrumb-trail a')).map(node => node.textContent),
      document.querySelector('[data-destination=trajectories]')?.getAttribute('aria-current'),
      document.querySelector('#trajectory-overview-panel')?.hidden,
      document.querySelector('#trajectory-snapshots-panel')?.hidden,
      document.querySelector('#trajectory-title')?.textContent.length > 0,
      Math.round(document.querySelector('#context-bar').getBoundingClientRect().height),
    ]`),
    [
      ['Overview', 'Records', 'Snapshots', 'Provenance'], 'Trajectory',
      ['Sessions', '12 Participants'], null, false, true, true, 38,
    ],
    'a native Trajectory is owned by its Session and opens its own contextual navigation',
  );
  assert.equal(
    await evaluate(`document.querySelector('[data-destination=sessions]')?.getAttribute('aria-current')`),
    'page',
    'a native Trajectory keeps its owning destination highlighted',
  );
  await command('Page.navigate', {url: `${origin}/trajectories/${encodeURIComponent(importedTrajectoryId)}`});
  await waitFor('document.querySelector("#trajectory-shell")?.hidden === false');
  await waitFor(`document.querySelector('#breadcrumb-trail')?.dataset.state === 'ready'`);
  assert.deepEqual(
    await evaluate(`[
      Array.from(document.querySelectorAll('#breadcrumb-trail a')).map(node => node.textContent),
      document.querySelector('[data-destination=trajectories]')?.getAttribute('aria-current'),
      document.querySelector('#trajectory-title')?.textContent,
      Math.round(document.querySelector('#context-bar').getBoundingClientRect().height),
    ]`),
    [['Trajectories'], 'page', 'external-run-104', 38],
    'an imported Trajectory is owned by the Trajectories destination rather than a Session',
  );
  await command('Page.navigate', {url: `${origin}/trajectories/${encodeURIComponent(trajectoryId)}`});
  await waitFor('document.querySelector("#trajectory-shell")?.hidden === false');
  await waitFor('document.querySelectorAll("#context-nav [data-context-item]").length === 4');
  await evaluate('document.querySelector("[data-context-item=snapshots]").click()');
  await waitFor(`document.querySelector('#trajectory-snapshots-panel')?.hidden === false &&
    location.pathname.endsWith('/snapshots')`);
  assert.deepEqual(
    await evaluate(`[
      location.pathname.endsWith('/snapshots'),
      document.querySelectorAll('#trajectory-snapshots .trajectory-segment').length,
      document.querySelector('#trajectory-snapshots .trajectory-segment p')?.textContent.includes('frozen'),
      document.querySelector('#trajectory-records')?.textContent.includes('payload'),
      Math.round(document.querySelector('#context-bar').getBoundingClientRect().height),
    ]`),
    [true, 1, true, false, 38],
    'trajectory Snapshots shows immutable boundaries without sensitive record payloads or a height change',
  );
  await evaluate('document.querySelector("[data-context-item=provenance]").click()');
  await waitFor(`location.pathname.endsWith('/provenance') &&
    !document.querySelector('#trajectory-provenance')?.textContent.includes('Loading provenance')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#trajectory-provenance-panel')?.hidden,
      document.querySelector('#trajectory-provenance')?.textContent.includes('Native session evidence'),
      document.querySelector('#trajectory-provenance')?.textContent.includes('Trajectory Digest'),
      document.querySelectorAll('[data-destination]').length,
    ]`),
    [false, true, true, 4],
    'contextual training provenance stays inside the Trajectory rather than becoming a destination',
  );
  await command('Page.reload');
  await waitFor('document.querySelector("#trajectory-shell")?.hidden === false && document.querySelector("#trajectory-provenance-panel")?.hidden === false');
  assert.equal(
    await evaluate('location.pathname'),
    `/trajectories/${trajectoryId}/provenance`,
    'a trajectory tab survives a deep-link refresh',
  );
  await evaluate(`(() => {
    window.__skeletonStates = [];
    const bar = document.querySelector('#context-bar');
    const trail = document.querySelector('#breadcrumb-trail');
    window.__skeletonObserver = new MutationObserver(() => window.__skeletonStates.push([
      trail.dataset.state, Math.round(bar.getBoundingClientRect().height)]));
    window.__skeletonObserver.observe(trail, {childList: true, attributes: true});
  })()`);
  await evaluate(`document.querySelector('[data-context-item=records]').click()`);
  await waitFor(`location.pathname.endsWith('/records') && window.__skeletonStates.some(state => state[0] === 'ready')`);
  assert.deepEqual(
    await evaluate(`(() => {
      window.__skeletonObserver.disconnect();
      const states = window.__skeletonStates;
      return [
        states.some(state => state[0] === 'loading'),
        states.at(-1)[0],
        [...new Set(states.map(state => state[1]))],
      ];
    })()`),
    [true, 'ready', [38]],
    'ancestry loads through a same-height skeleton so the reserved breadcrumb space never changes',
  );
  await command('Emulation.setDeviceMetricsOverride', {width: 420, height: 800, deviceScaleFactor: 1, mobile: true});
  await command('Page.navigate', {url: `${origin}/sessions/${encodeURIComponent(nestedSessionId)}`});
  await waitFor('document.querySelectorAll("#breadcrumb-trail > a").length === 2');
  assert.deepEqual(
    await evaluate(`[
      Array.from(document.querySelectorAll('#breadcrumb-trail > a')).map(node => node.textContent),
      Array.from(document.querySelectorAll('.breadcrumb-overflow-menu a')).map(node => node.textContent),
      document.querySelector('.breadcrumb-ellipsis')?.getAttribute('aria-label'),
      document.querySelector('.breadcrumb-overflow-menu')?.hidden,
      document.querySelector('#breadcrumb-trail').scrollWidth <= document.querySelector('#breadcrumb-trail').clientWidth + 1,
      getComputedStyle(document.querySelector('#context-nav')).flexDirection,
    ]`),
    [
      ['Experiments', 'Routine request'], ['Support response evaluation'],
      'Show 1 hidden ancestor', true, true, 'row',
    ],
    'a narrow viewport keeps the root and nearest ancestor and collapses the rest into an accessible menu',
  );
  await evaluate(`document.querySelector('.breadcrumb-ellipsis').click()`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('.breadcrumb-overflow-menu')?.hidden,
      document.querySelector('.breadcrumb-ellipsis')?.getAttribute('aria-expanded'),
    ]`),
    [false, 'true'],
    'the collapsed ancestry opens on demand',
  );
  await command('Emulation.setDeviceMetricsOverride', {width: 1440, height: 900, deviceScaleFactor: 1, mobile: false});
  await waitFor(`document.querySelectorAll('#breadcrumb-trail > a').length === 3`);
  await evaluate('document.querySelector("[data-destination=sessions]").click()');
  await waitFor('document.querySelector("#home-view")?.hidden === false');
  for (const [width, label] of [[768, 'tablet'], [375, 'mobile'], [241, 'narrow-mobile']]) {
    await command('Emulation.setDeviceMetricsOverride', {width, height: 800, deviceScaleFactor: 1, mobile: width < 500});
    await waitFor('document.querySelector("#home-view")?.hidden === false');
    assert.equal(
      await evaluate('document.documentElement.scrollWidth === window.innerWidth'),
      true,
      `the Sessions index avoids horizontal document scrolling at the ${label} viewport`,
    );
    assert.equal(
      await evaluate(`(() => {
        const brand = document.querySelector('.sidebar-brand');
        const tabs = document.querySelector('.global-tabs');
        const actions = document.querySelector('.navbar-actions');
        return (getComputedStyle(brand).visibility === 'hidden' || brand.scrollWidth <= brand.clientWidth) &&
          brand.getBoundingClientRect().right <= tabs.getBoundingClientRect().left + 1 &&
          tabs.getBoundingClientRect().right <= actions.getBoundingClientRect().left + 1;
      })()`),
      true,
      `the brand, global destinations, and attach controls do not overlap at the ${label} viewport`,
    );
    assert.equal(
      await evaluate(`document.querySelector('#home-session-list').scrollWidth >
        document.querySelector('#home-session-list').clientWidth`),
      true,
      `The session table scrolls horizontally at the ${label} viewport`,
    );
    assert.equal(
      await evaluate(`getComputedStyle(document.querySelector('.home-session-columns > :nth-child(6)')).display !== 'none'`),
      true,
      `The session table keeps Turns available at the ${label} viewport`,
    );
    if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
      const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
      writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/home-${label}.png`, screenshot.data, 'base64');
    }
  }
} finally {
  socket.close();
}
