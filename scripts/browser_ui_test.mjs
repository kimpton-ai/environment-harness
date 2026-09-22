import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';

const [origin, debugPort] = process.argv.slice(2);
if (!origin || !debugPort) throw new Error('origin and debug port are required');

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
  await command('Page.navigate', {url: `${origin}/home`});
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
    await secondTabConnects(`${origin}/home`),
    true,
    'A second local viewer tab connects without sharing browser storage',
  );
  assert.equal(new URL(await evaluate('location.href')).pathname, '/home', 'the session index has a stable URL');
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#home')?.tagName,
      document.querySelector('.global-breadcrumb')?.getAttribute('aria-label'),
      getComputedStyle(document.querySelector('#home')).borderBottomWidth,
      document.querySelector('.home-heading .eyebrow')?.textContent,
      getComputedStyle(document.querySelector('.home-heading .eyebrow')).textTransform,
      document.querySelector('#home svg') === null,
      getComputedStyle(document.querySelector('#home'), '::before').content,
    ]`),
    ['A', 'Breadcrumb', '0px', 'Home', 'none', true, 'none'],
    'Home uses a text breadcrumb without an icon or leading separator',
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
    await evaluate(`document.querySelector('#home-selection').getBoundingClientRect().top <
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
      document.querySelector('.experiment-name')?.getAttribute('href')?.startsWith('/experiment/'),
      document.querySelectorAll('.scenario-row[data-scenario-id]').length,
      document.querySelectorAll('.standalone-session[data-session-id]').length,
    ]`),
    ['/home', 'A', true, 0, 3],
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
  await waitFor(`location.pathname.startsWith('/experiment/')`);
  assert.deepEqual(
    await evaluate(`[
      /^\\/experiment\\/[^/]+$/.test(location.pathname),
      document.querySelector('.home-heading h1')?.textContent,
      Array.from(document.querySelectorAll('#experiment-tabs [role=tab]')).map(tab => tab.textContent),
      document.querySelector('#experiment-tabs')?.hidden,
      document.querySelector('#experiment-overview')?.hidden,
      document.querySelector('#home-list-content')?.hidden,
      document.querySelector('#experiment-frozen-summary')?.textContent.includes('synthetic-protocol@1'),
      getComputedStyle(document.querySelector('#home-view')).backgroundColor,
    ]`),
    [true, 'Support response evaluation', ['Overview', 'Scenarios', 'Sessions'], false, false, true, true, 'rgb(255, 255, 255)'],
    'the experiment name opens its configuration overview with subordinate navigation on the session canvas',
  );
  await evaluate(`document.querySelector('[data-experiment-tab="scenarios"]').click()`);
  await waitFor(`location.pathname.endsWith('/scenarios')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelectorAll('.experiment-scenario-row').length,
      document.querySelector('.experiment-scenario-row')?.textContent.includes('Routine request'),
      document.querySelector('[data-experiment-tab="scenarios"]')?.getAttribute('aria-selected'),
    ]`),
    [2, true, 'true'],
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
    ]`),
    ['Routine request', true, true, 'View Sessions'],
    'a scenario opens its immutable inputs and navigation to produced sessions',
  );
  await evaluate(`document.querySelector('#scenario-view-sessions').click()`);
  await waitFor(`location.pathname.endsWith('/sessions')`);
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('.home-list-heading h2')?.textContent,
      document.querySelectorAll('.scenario-session[data-session-id]').length,
      document.querySelector('#session-search')?.value,
    ]`),
    ['Experiment Sessions', 2, 'easy-case'],
    'View Sessions opens the session list filtered to the selected scenario',
  );
  await evaluate(`document.querySelector('.scenario-session .session-name').click()`);
  await waitFor(`location.pathname.startsWith('/session/')`);
  assert.deepEqual(
    await evaluate(`[
      location.pathname.endsWith('/overview'),
      document.querySelector('.breadcrumb-link')?.textContent,
      document.querySelector('.breadcrumb-link')?.getAttribute('href')?.startsWith('/experiment/'),
      document.querySelector('.breadcrumb-current')?.textContent,
    ]`),
    [true, 'Support response evaluation', true, 'Routine request · Trial 1'],
    'a produced session keeps its canonical route and links back to its parent experiment',
  );
  await evaluate(`document.querySelector('.breadcrumb-link').click()`);
  await waitFor(`/^\\/experiment\\/[^/]+$/.test(location.pathname)`);
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/experiment.png`, screenshot.data, 'base64');
  }
  await evaluate(`document.querySelector('#home').click()`);
  await waitFor(`location.pathname === '/home'`);
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
    `/session/${openedSessionId}/overview`,
    'opening an environment session updates the URL',
  );

  assert.equal(
    await evaluate('document.querySelector("#context-title")?.textContent'),
    '12 Participants',
    'large participant rosters use a compact session title',
  );
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('.page-context .eyebrow')?.textContent,
      getComputedStyle(document.querySelector('.page-context .eyebrow')).textTransform,
      getComputedStyle(document.querySelector('.page-context .eyebrow')).fontSize,
      getComputedStyle(document.querySelector('.home-heading .eyebrow')).fontSize,
    ]`),
    ['Session', 'none', '12px', '12px'],
    'page eyebrows use consistent, readable mixed-case typography',
  );
  assert.deepEqual(
    await evaluate(`[
      document.querySelector('#home span')?.textContent,
      getComputedStyle(document.querySelector('.navbar-actions')).display,
      Array.from(document.querySelectorAll('#breadcrumb-trail > *')).map(node => node.textContent).join(''),
      document.querySelector('[aria-label="Selected Session Navigation"]')?.previousElementSibling?.classList.contains('page-context'),
      document.querySelector('#home svg') === null,
      getComputedStyle(document.querySelector('#home')).fontSize,
      getComputedStyle(document.querySelector('.breadcrumb-current')).fontSize,
    ]`),
    ['Home', 'flex', '/12 Participants', true, true, '12px', '12px'],
    'the navbar and local navigation change when a user enters an environment session',
  );
  assert.equal(
    await evaluate('parseFloat(getComputedStyle(document.querySelector(".page-context")).paddingLeft) >= 40'),
    true,
    'the selected environment session uses the same generous side gutter as Home',
  );
  assert.deepEqual(
    await evaluate(`Array.from(document.querySelectorAll('[data-session-tab]')).map(node => node.textContent)`),
    ['Overview', 'Turns', 'Progression', 'Reports'],
    'a selected environment session separates summary, inspection, progression, and report records',
  );
  assert.equal(
    await evaluate('document.querySelector("[data-session-tab=overview]")?.getAttribute("aria-selected")'),
    'true',
    'opening a session starts at its session-level overview',
  );
  await waitFor('document.querySelectorAll("#evaluation-metrics .evaluation-metric:not(.evaluation-metric-heading)").length === 3');
  assert.equal(
    await evaluate(`document.querySelector('#session-overview')?.textContent.includes('Max Turns') &&
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
  await evaluate('document.querySelector("[data-session-tab=turns]").click()');
  await waitFor('location.pathname.endsWith("/turns")');
  await waitFor(`window.__viewerRequests.includes('/v1/environments/${openedSessionId}') &&
    window.__viewerRequests.includes('/v1/environments/${openedSessionId}/reports')`);
  assert.equal(
    await evaluate(`window.__viewerRequests.includes('/v1/environments/${openedSessionId}') &&
      window.__viewerRequests.includes('/v1/environments/${openedSessionId}/events') &&
      window.__viewerRequests.includes('/v1/environments/${openedSessionId}/reports')`),
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

  await evaluate('document.querySelector("[data-session-tab=reports]").click()');
  await waitFor('location.pathname.endsWith("/reports")');
  assert.equal(
    await evaluate(`document.querySelectorAll('#report-history .report-record').length === 24 &&
      document.querySelectorAll('#report-history .report-line-card').length === 3 &&
      document.querySelectorAll('#report-history .report-latest-metrics .metric').length === 3 &&
      Array.from(document.querySelectorAll('#report-history .report-record')).every(node => !node.open) &&
      Array.from(document.querySelectorAll('#report-history .report-record-body > details > summary')).every(node => node.textContent === 'Raw')`),
    true,
    'Reports summarizes latest values and progression while preserving expandable versioned records',
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

  await evaluate(`(() => { window.__viewerRequests = []; document.querySelector('#home').click(); })()`);
  await waitFor('document.querySelector("#home-view")?.hidden === false');
  await waitFor(`window.__viewerRequests.includes('/v1/environments') &&
    window.__viewerRequests.includes('/v1/activity/snapshot')`);
  assert.equal(new URL(await evaluate('location.href')).pathname, '/home', 'Home updates the URL');
  assert.deepEqual(
    await evaluate(`[new URL(location.href).searchParams.getAll('environment').length,
      document.querySelector('#home-selection').hidden,
      document.querySelectorAll('#home-session-list input[type=checkbox]:checked').length]`),
    [0, true, 0],
    'clicking the Home breadcrumb clears the current selection and writes a clean Home URL',
  );
  assert.equal(
    await evaluate(`window.__viewerRequests.includes('/v1/environments') &&
      window.__viewerRequests.includes('/v1/activity/snapshot')`),
    true,
    'entering Home refreshes the environment-session catalog and activity snapshot',
  );
  await evaluate('history.go(-5)');
  await waitFor(`location.pathname === '/home' &&
    new URL(location.href).searchParams.getAll('environment').length === 1 &&
    document.querySelectorAll('#home-session-list input[type=checkbox]:checked').length === 1`);
  assert.equal(
    await evaluate(`document.querySelector('#home-selection-count')?.textContent`),
    '1 session selected',
    'browser Back restores the selection encoded in the earlier Home history entry',
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
  assert.equal(new URL(await evaluate('location.href')).pathname, '/compare', 'session comparison has a stable URL');
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
  assert.equal(
    await evaluate('document.querySelector("#home span")?.textContent'),
    'Home',
    'cross-session comparison remains a Home-level destination',
  );
  if (process.env.BROWSER_UI_SCREENSHOT_DIR) {
    const screenshot = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    writeFileSync(`${process.env.BROWSER_UI_SCREENSHOT_DIR}/compare.png`, screenshot.data, 'base64');
  }
  await evaluate('document.querySelector("#home").click()');
  await evaluate('history.back()');
  await waitFor('document.querySelector("#compare-sessions-view")?.hidden === false');
  await evaluate('history.back()');
  await waitFor('document.querySelector("#home-view")?.hidden === false');
  for (const [width, label] of [[768, 'tablet'], [375, 'mobile'], [241, 'narrow-mobile']]) {
    await command('Emulation.setDeviceMetricsOverride', {width, height: 800, deviceScaleFactor: 1, mobile: width < 500});
    await waitFor('document.querySelector("#home-view")?.hidden === false');
    assert.equal(
      await evaluate('document.documentElement.scrollWidth === window.innerWidth'),
      true,
      `Home avoids horizontal document scrolling at the ${label} viewport`,
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
      `The brand, breadcrumb, and attach controls do not overlap at the ${label} viewport`,
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
