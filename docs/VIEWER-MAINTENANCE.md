# How to maintain the viewer

The EnvironmentHarness viewer is a same-origin, read-only browser application packaged inside the
Python wheel. TypeScript supplies its behavior and data projections; checked-in HTML and CSS supply
its document structure and visual system.

## Source and artifact map

```text
packages/typescript/src/app.ts       application state, routing, and rendering
packages/typescript/src/client.ts    authenticated HTTP client
packages/typescript/src/timeline.ts  evidence and turn projections
packages/typescript/src/types.ts     shared contract types
                 │
                 │ npm run build
                 ▼
packages/typescript/dist/*.js
                 │
                 │ scripts/build_viewer.py
                 ▼
src/environment_harness/viewer/*.js  checked-in generated wheel assets

src/environment_harness/viewer/index.html  viewer document and route shell
src/environment_harness/viewer/style.css  viewer visual system
```

Edit TypeScript in `packages/typescript/src/`. Do not hand-edit generated JavaScript under
`src/environment_harness/viewer/`. `scripts/build_viewer.py --check` fails when the two copies
drift.

`index.html` and `style.css` are direct sources; they are not generated. The Python wheel includes
all files under `src/environment_harness/viewer/`.

## Runtime contract

### Routes

The FastAPI application serves the same viewer shell at:

- `/` and `/home`;
- `/compare`;
- `/experiment/{experiment}`;
- `/experiment/{experiment}/scenarios` and `/experiment/{experiment}/scenarios/{scenario}`;
- `/experiment/{experiment}/sessions`;
- `/experiment/{experiment}/training` when a matching frozen dataset exists;
- `/trajectory/{trajectory}` for imported-trajectory inspection;
- `/session/{environment}`; and
- `/session/{environment}/overview`, `/turns`, `/progression`, or `/reports`.

The application uses `history.pushState` and restores routes on browser back/forward navigation.
Server-side fallback routes are required so refresh and deep links render the application instead
of returning `404`.

### Authentication modes

The browser reads `GET /viewer/config` before constructing its API client.

- `local`: the packaged CLI server exposes `POST /local/connect` only to the exact loopback peer,
  host, and origin. The browser exchanges an in-memory credential on every load. Connection failure
  shows a retry banner, never the supplier credential form.
- `credential`: an embedded supplier application omits `/local/connect`. The browser asks for a
  bearer credential and keeps it only in page memory.

Do not place credentials in HTML, URLs, generated configuration, browser storage, logs, or test
screenshots. All `/v1` routes remain authenticated in both modes.

### Live updates

Home loads the environment-session list and activity snapshot together. Experiment disclosures
expand directly into their environment sessions; the experiment name separately opens its stable
Overview route. Meaningful scenarios have a dedicated experiment tab and clickable detail routes;
an empty/default scenario does not add navigation. `View Sessions` moves to the experiment's flat
session list with a scenario filter instead of inserting a label-only layer. Child rows expose scenario
and trial identity, status, turn progress, participants and latest activity. It then reads finite
server-sent-event activity pages. After three activity-stream failures it falls back to the JSON
activity endpoint with backoff. Changes refresh the list and any selected environment session.

While an environment session is visible, the viewer also checks for new evidence every three
seconds. Manual Refresh reloads the hierarchy and current route. Preserve cursors and the selected
session when changing this behavior; a viewer disconnect must never block evidence writes.

### Evidence projection parity

`packages/typescript/src/timeline.ts` mirrors `src/environment_harness/presentation.py`. Keep their
turn grouping, participant attribution, inherited-record reconstruction, missing-state language,
operation intent/receipt descriptions, and field names aligned. The browser is a projection of
recorded evidence, not a second authority. User-facing receipt summaries replace identifier
underscores with spaces while preserving the recorded receipt unchanged.

The Progression page derives time series from executed rewards, public numeric signals, and score
reports. Missing score revisions remain gaps rather than invented values. Comparison only pools
compatible metric definitions and units.

Home lists imported trajectories separately from experiments and standalone environment sessions.
The trajectory route shows collection health, execution/outcome/termination distinctions, segment
continuity, native time, and a bounded first record page. Extension data, raw token arrays, rendered
requests, and model responses stay suppressed. The Training route shows immutable dataset and
recorded integration provenance; it never invokes training code.

## Make a viewer change

### Prerequisites

Install Python 3.12 or later, uv 0.12.0, Node.js 22, npm 11.17, and Chrome or Chromium for the
browser checks.

```sh
uv sync --extra server
npm ci --ignore-scripts --prefix packages/typescript
```

### 1. Change the authoritative source

Use this routing table:

| Change | Primary files |
| --- | --- |
| Page structure or accessible labels | `src/environment_harness/viewer/index.html` |
| Layout, typography, responsive behavior | `src/environment_harness/viewer/style.css` |
| Navigation, rendering, filters, live updates | `packages/typescript/src/app.ts` |
| API requests or browser error handling | `packages/typescript/src/client.ts` |
| Turn grouping, event summaries, charts | `packages/typescript/src/timeline.ts` and matching Python projection |
| API response shape | contracts, generated TypeScript types, server, and clients |
| Viewer authentication | server, local-viewer seam, application startup, browser code, and security tests |

Follow the [Viewer style guide](STYLE-GUIDE.md). Use sentence case or natural title case, preserve
recorded identifiers, and avoid CSS uppercase transformations.

### 2. Add or change the test first

Choose the narrowest test that proves the behavior:

- `packages/typescript/tests/*.test.mjs` for client and projection logic;
- `scripts/browser_ui_test.mjs` for rendered layout, interaction, routing, scrolling, selection,
  and live hierarchy behavior;
- `scripts/check_browser_security.py` for authentication, credential exposure, origin, and content
  security boundaries;
- Python server tests for routes, response contracts, and authorization; and
- distribution checks when packaged files change.

Layout assertions should test an observable relationship or behavior rather than a brittle pixel
snapshot. Screenshot output is for human review, not the only regression test.

### 3. Build the checked-in viewer assets

```sh
make viewer
```

This installs the locked TypeScript dependencies, compiles the package, and copies `app.js`,
`timeline.js`, `client.js`, and `types.js` into the Python package. Review both the TypeScript source
and generated JavaScript diff.

### 4. Run focused checks

```sh
npm run typecheck --prefix packages/typescript
npm test --prefix packages/typescript
uv run --no-sync python scripts/build_viewer.py --check
uv run --no-sync python scripts/check_browser_ui.py
uv run --no-sync python scripts/check_browser_security.py
```

To capture the browser-check states for visual review:

```sh
mkdir -p .local/viewer-screenshots
BROWSER_UI_SCREENSHOT_DIR=.local/viewer-screenshots \
  uv run --no-sync python scripts/check_browser_ui.py
```

The screenshot directory is local and must not contain private environment sessions or be
committed as release evidence.

The four screenshots rendered in `README.md` are public documentation assets under `docs/assets/`,
not browser-test output. Refresh them when the visible hierarchy, experiment configuration, or
report layout changes:

- create Home and experiment data with `quickstart --turns 3` and
  `examples/custom_environment_experiment.py` in one temporary store, then use a separate
  `quickstart --turns 8` store for the Progression image;
- run the packaged server against each store and capture a 1440 by 900 viewport;
- capture Home with one experiment expanded, the custom experiment Overview, a session's
  Progression tab with a real multi-point series, and one child session's Reports tab;
- use only the repository's synthetic records and verify that the browser console is clean; and
- keep the stable filenames `environment-harness-home.png`, `environment-harness-experiment.png`,
  `environment-harness-progression.png`, and `environment-harness-reports.png` so the PyPI-rendered
  README does not drift.

Review these images as documentation, not as pass/fail test evidence. Interactive behavior remains
covered by the browser checks above.

### 5. Run the complete checks

```sh
make check
make build
```

`make check` enforces viewer drift, browser behavior, TypeScript compilation and tests, Python
tests, formatting, typing, and generated-contract drift. `make build` verifies that the wheel,
source distribution, and TypeScript artifact contain the intended files.

## Review checklist

- The empty state tells the user how to create or attach an environment session.
- Home separates experiments from standalone environment sessions, expands experiments directly
  into useful child rows, and scales through scrolling and pagination.
- Experiment names and disclosure controls remain separate: the name opens details while the
  disclosure uses visible `View N sessions` / `Hide sessions` text with `aria-expanded` and
  `aria-controls`.
- Session tabs remain subordinate to the selected environment session.
- Turns preserve the relationship between starting state, participant evidence, and environment
  resolution.
- Progression shows change over time without inventing missing points.
- Reports summarize scores and findings before exposing raw records.
- Session comparison explains compatible aggregates and keeps incompatible metrics separate.
- Imported trajectories distinguish collection, execution, termination, and verified outcome;
  show gaps/backlog/capture failures; and render unknown extensions as inert summaries.
- Training navigation appears only when a matching dataset exists and never exposes raw inference
  detail or a control that executes a trainer.
- Keyboard focus, labels, live regions, scrolling containers, and narrow layouts still work.
- Refresh, a copied deep link, browser back/forward, and a second local tab still connect.
- Local mode never shows the credential form; supplier mode never exposes the workspace before
  authentication.
- Credentials and private event data do not appear in HTML, URLs, logs, generated configuration,
  or screenshots.
- The CLI timeline and browser timeline describe the same recorded evidence.

## Change-impact map

| If you change… | Also update or verify… |
| --- | --- |
| A viewer route or tab | FastAPI fallback routes, route parser, browser navigation tests, and [Deployment](DEPLOYMENT.md) |
| Authentication mode | `docs/PROTOCOL.md`, local and supplier security tests, browser tests, and generated OpenAPI if public routes change |
| API shape | server, Python and TypeScript clients, schemas, OpenAPI, API reference, compatibility notes, and viewer callers |
| Timeline semantics | Python presentation parity, TypeScript projections, examples, and compatibility notes |
| Packaged assets | `build_viewer.py`, distribution manifest checks, installed-wheel smoke test, and release guide |
| Typography or shared components | style guide plus every route that uses the component |
| Activity refresh behavior | cursor recovery, stream fallback, selected-session refresh, and disconnect behavior |

## Troubleshooting

### `viewer drift: app.js`

The TypeScript build output differs from the checked-in Python asset. Run `make viewer`, review the
generated diff, and commit source and generated files together.

### The browser check cannot find Chrome

Install Chrome or Chromium in a supported location. The browser check intentionally fails instead
of silently skipping interactive coverage.

### A deep link works through navigation but fails on refresh

Add or correct the matching FastAPI viewer fallback route. Do not solve this with a generic static
server; the packaged application owns the route shell.

### The viewer does not update while a session runs

Check the authenticated activity snapshot and event endpoints, then the session event cursor. The
viewer falls back from the event stream to JSON activity polling after repeated failures, so test
both paths before changing the refresh interval.

## Related documentation

- [How to run EnvironmentHarness](DEPLOYMENT.md)
- [How to release EnvironmentHarness](RELEASING.md)
- [API reference](API-REFERENCE.md)
- [Protocol](PROTOCOL.md)
- [Session reliability and compatibility](COMPATIBILITY.md)
