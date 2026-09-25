# How to release EnvironmentHarness

EnvironmentHarness publishes one coordinated version across the Python package, packaged viewer,
and TypeScript client artifact. Releases are prepared in reviewed pull requests and published from
a protected tag through GitHub Actions and PyPI Trusted Publishing.

## Release artifacts

One release produces:

- a Python wheel containing the SDK, server, viewer assets, SQL migrations, and typing marker;
- a Python source distribution containing source, contracts, documentation, and examples;
- a TypeScript client tarball attached to the GitHub release;
- software bills of materials, checksums, and GitHub build-provenance attestations; and
- a GitHub release followed by the wheel and source distribution on PyPI.

The TypeScript client is currently a GitHub release asset, not an npm registry publication.

## Prerequisites

Use Python 3.12 or later, uv 0.12.0, Node.js 22, and npm 11.17. Start from a clean release branch
whose `dist/` directory contains no artifacts from another version.

Repository administrators must first configure the protected branch, tag, `release-tag`,
`github-release`, and `pypi` environments plus the PyPI Trusted Publisher described in
[Required repository settings](../.github/REPOSITORY-SETTINGS.md). The publisher must target:

- GitHub owner: `kimpton-ai`
- Repository: `environment-harness`
- Workflow: `release.yml`
- Environment: `pypi`
- PyPI project: `environment-harness`

No PyPI password or API token belongs in GitHub Actions.

Bind every required `main` status check to the GitHub Actions integration as its expected source.
CI workflow changes require security CODEOWNER review, and path-based compatibility routing must
fail closed when GitHub returns incomplete or malformed changed-file data.

Keep `v*` tags immutable with update and deletion restrictions, but do not restrict creation: the
built-in Actions app cannot use a `github-actions[bot]` user bypass. Tag pushes never start this
publisher, and any unexpected version tag is a security incident.

## Choose the version

Python versions follow PEP 440. The TypeScript artifact uses the corresponding SemVer spelling:

| Stage | Python | TypeScript |
| --- | --- | --- |
| Alpha | `X.Y.Za1` | `X.Y.Z-alpha.1` |
| Beta | `X.Y.Zb1` | `X.Y.Z-beta.1` |
| Release candidate | `X.Y.Zrc1` | `X.Y.Z-rc.1` |
| Final | `X.Y.Z` | `X.Y.Z` |

Use a release candidate when the release needs real installation and integration feedback before
the stable version. PyPI prereleases are immutable production-PyPI releases, but ordinary
`pip install environment-harness` does not select them.

### Frozen `0.3.0rc1` manifest

The trajectory-contract program uses exactly one coordinated candidate, `0.3.0rc1`, followed by
`0.3.0`. Do not introduce alpha, beta, or routine additional candidates to stage internal
workstreams. **This manifest is frozen.** `0.3.0` publishes the same package set and the same
public feature surface; an additional candidate is permitted only to fix a qualification failure
found in `0.3.0rc1`, never to add scope.

**Packages.** Three artifacts on one coordinated version: the Python wheel, the Python source
distribution, and the TypeScript client tarball attached to the GitHub release. No npm registry
publication.

**Runtime.** Python `>=3.12`. Base dependencies `pydantic>=2.12.5,<3` and `jsonschema>=4.26,<5`.
Extras: `server`, `postgres`, `modal`, `pettingzoo`, `signatures`, `mcp`, `verifiers`.

**Public Python surface.** The 24 names in `environment_harness.__all__`. `EnvironmentHarness` is
the only local-execution facade; `EnvironmentSession` and `SessionControl` are its typed handles.
No public callable exposes a principal, access context, role, kind, or permission.

**Wire contracts.** Protocol `environment-session.v1`; portable resources
`environmentharness.dev/v1alpha1`. The canonical short HTTP hierarchy with a typed management-list
envelope, ETags, `Location` headers, one stable error taxonomy, and the three declared capabilities
`historical-ingestion`, `participant-credentials`, `local-viewer`. All 40 frozen 0.2 operations are
classified exactly once in `contracts/migrations/http-0.2-to-0.3.json`.

**Generated artifacts.** 36 JSON Schemas and `contracts/openapi.json` under `contracts/`; the
checked-in viewer assets under `src/environment_harness/viewer/`; the generated
[HTTP migration](HTTP-MIGRATION.md) table. Every one has a `--check` drift gate.

**Storage.** Migrations `001` through `006`, applied automatically for SQLite and through
`PostgresEvidenceStore.initialize()` for PostgreSQL.

**Credentials.** Three issuable server-owned policies — `admin`, `viewer`, `participant` — plus the
non-issuable in-process `trusted-local` context.

**Viewer.** Four global destinations, one contextual left navigation per resource, ancestry
breadcrumbs, and the reserved-height context bar.

**Documentation.** `README.md`, `packages/typescript/README.md`, `CHANGELOG.md`, and the 20 guides
under `docs/`.

**Explicitly excluded.** Parquet export, RLlib conversion and external-environment support, TRL
integration, live OpenEnv training, remote training workers and distributed decision workers, and
the separately owned **Pluggable Decision-Selection Seam**. None of these is a dependency of the
trajectory foundation. The bounded Verifiers legacy bridge is included and pinned to
`>=0.3.1,<0.4`.

A separately installable decision runtime keeps its own version and release gate. If it is ever
added to a manifest it ships in the same candidate/final release event rather than on its own
timeline. See [Decision runtime](DECISION-RUNTIME.md).

### Downstream-impact appendix

`0.3.0rc1` is broadly incompatible with `0.2.x`. This appendix is the complete list of breaking
changes and their required migrations, derived from this repository's own diff, so it can be
written and reviewed here without copying consumer code, deployment state, or credentials into the
repository or the release artifacts.

**Last compatible pin for any consumer that has not migrated: `environment-harness==0.2.4rc2`.**
An unqualified deployment must stay at that pin rather than upgrade partway.

No downstream consumer is known to depend on `0.2.x` at the time of this freeze. Add a row here for
any breaking change a later release introduces, and re-verify the table against any consumer that
adopts the SDK before `0.3.0`.

| Change | Affected API or stored representation | Required migration |
| --- | --- | --- |
| `Principal` and the four-role model removed | Every SDK call and HTTP request that constructed or forwarded a principal; `x-roles` in generated clients | Delete principal construction. Use `EnvironmentHarness` in process; send only a bearer credential remotely. No adapter is possible — the type is gone. |
| Migration `005_credential_policies` deletes every credential row | The `credentials` table; all issued bearer tokens | Reissue before or immediately after upgrade. Old tokens return `401`. Tokens cannot be converted. |
| `/v1/environments/*` replaced by the short hierarchy | Every HTTP route and generated client method | Apply the per-operation mapping in `contracts/migrations/http-0.2-to-0.3.json` and [HTTP migration](HTTP-MIGRATION.md). Regenerate clients; do not hand-edit paths. |
| `GET /v1/environment` removed | Consumers that fetched a standalone `EnvironmentSpec` | Read the spec frozen inside the Experiment and inherited by its Sessions. |
| `Trajectory.status.records` removed | Any consumer that read records from the status object | Page `GET /v1/trajectories/{id}/records`, or `records_page`/`stream_records` in process. A materializing shim reintroduces the unbounded read this change removed; do not write one. |
| `ActivitySnapshot` → `ActivityHierarchy`; `/v1/activity/snapshot` → `/v1/activity/hierarchy` | Activity recovery callers and stored client types | Rename. Field semantics are unchanged. |
| Management collections return `items`/`nextCursor`/`links` with an opaque cursor | Every list response and any consumer that persisted a list cursor | Read `items`; treat the cursor as opaque and never derive or store a synthetic one. Durable evidence and activity feeds keep their integer cursors. |
| `GET /v1/sessions/{id}/invocations` returns `items` (was `work`) | Agent-work readers | Rename the key. |
| Snapshot and dataset export uses content negotiation on `/records` | `/export` verb callers | Request `/records` with the desired media type. |
| Typed environment factories replace persisted import paths | `session_runs` rows and any consumer that wrote an import path | Configure factories once per harness. Migration `006_scheduler_recovery` adds `environment_id`, `environment_version`, `spec_digest`, and `blocked_reason`; legacy rows project read-only without rewrite. A missing or mismatched factory leaves a Session durably `blocked` rather than failing at execution time. |
| `EnvironmentHarness(environment_factory=, agent_factories=)` renamed | Every SDK construction site | Rename to `environment=` and `agents=`. Replace `environments=[A, B]` with `environment=[A, B]`. Read the durable blocked reason as `environment_not_configured`. |
| `SessionRunner` receives a typed `SessionControl` | Custom session runners | Replace `(session, environment, access, agents, turns=...)` with `(control, agents, turns=...)`. See [Environment authoring](AUTHORING.md). |
| `RunPolicy.inference_capture` defaults to `summary` | Recorded inference evidence and any consumer reading rendered requests, responses, token IDs, or log probabilities | Set `inference_capture="training"` with a training entitlement and a non-zero `max_inference_artifact_bytes`. Existing recorded evidence is unchanged; only new sessions are affected. |
| Viewer routes are plural and a detail root equals its Overview tab | Deep links, bookmarks, embedded links, `serve --open`, and `create_synthetic_showcase().review` | Rewrite links to `/overview`, `/experiments/{id}`, `/sessions/{id}`, `/trajectories/{id}`, `/comparisons`. Read `review.overview` instead of `review.home`. |

Two rows deliberately have no converter: the credential deletion and the `Principal` removal. The
project accepts a forced reissue and a hard compile break rather than carrying the discarded role
taxonomy or a principal compatibility mapper into `0.3`. Do not justify either by claiming tokens
are necessarily short-lived — the previous API allowed long TTLs.

## Prepare a release candidate

Release preparation is an explicitly dispatched workflow that changes the coordinated version on a
new branch and opens the required pull request. Run **Prepare release pull request** from GitHub
Actions on `main`, then choose:

- `bump`: `patch`, `minor`, or `major` for the first candidate on a release line; use `none` when
  advancing or finalizing an existing prerelease; and
- `stage`: `alpha`, `beta`, `rc`, `stable`, or `final`.

The workflow never merges or publishes. It runs the same `release_version.py prepare` command
documented below, commits only the coordinated version surfaces, and opens a `release/vX.Y.Z...`
branch for normal CI, CODEOWNER review, and independent approval.

### 1. Finish the changelog

Put every user-facing change under `## Unreleased` in `CHANGELOG.md`. Include compatibility or
migration guidance for intentionally incompatible changes.

### 2. Synchronize the release version

For local validation or recovery, the equivalent first-candidate command is:

```sh
python scripts/release_version.py prepare --bump patch --prerelease rc
```

The same command starts a new release line when the current metadata is a candidate on an older
line. For example, `--bump minor --prerelease rc` advances `0.2.4rc2` to `0.3.0rc1`; supplying a
bump starts the new line at candidate 1 instead of advancing the older line's candidate serial.

Use `minor` or `major` when appropriate. The command synchronizes:

- `pyproject.toml`;
- `src/environment_harness/__init__.py`;
- `uv.lock`;
- `packages/typescript/package.json` and its lockfile;
- TypeScript artifact examples; and
- `docs/STATUS.md`.

Do not edit those versions separately.

The equivalent command for another candidate on the same release line is:

```sh
python scripts/release_version.py prepare --prerelease rc
```

Prerelease stages may advance from alpha to beta to release candidate, but they cannot move
backwards. A published or otherwise pending metadata version must be released before preparing the
next version.

### 3. Validate the proposed tag

```sh
python scripts/release_version.py validate --release-tag vX.Y.ZrcN
```

### 4. Run the release checks

```sh
make setup
make viewer
make check
npm run typecheck --prefix packages/typescript
make build
make security
```

`make check` runs Python tests, coverage gates, Ruff, Pyright, schema and OpenAPI drift checks,
viewer drift, browser UI checks, and TypeScript tests. `make build` creates and validates the wheel,
source distribution, and TypeScript tarball. `make security` performs the full repository policy,
dependency, and audit checks.

### The fresh-environment walkthrough

`scripts/check_release.py` is the documentation-acceptance walkthrough, and it is a required check
rather than a manual procedure. It builds a throwaway virtual environment outside the checkout,
installs the pinned runtime dependencies and then the built wheel with `--no-deps`, and runs the
public journey against that installation with a filtered environment. Running it locally requires
`uv` and exactly one wheel in `dist/`:

```sh
uv build
uv run --no-sync python scripts/check_release.py
```

It records its result to `.local/release-check.json` and asserts, in order:

1. the imported package resolves inside the installed prefix, its metadata, `__version__`, and
   `environment-harness doctor` agree, and `py.typed` and the SQL migrations ship;
2. the shipped examples run and produce their documented totals and single-lineage comparison;
3. `EnvironmentHarness` records a native session through a custom `SessionRunner` that stops inside
   its budget, leaving the session resumable;
4. a historical source is registered and ingested, an identical retry is idempotent, and a **second
   process** resumes from the acknowledged position and hash — the restart-safe cursor is exercised,
   not asserted;
5. an immutable snapshot freezes and re-exports byte-identically, while a dataset built from the
   same evaluation-only evidence is refused for lacking a training entitlement;
6. the packaged server serves the four global viewer destinations, a session deep link, and a
   trajectory tab, and returns `404` for the removed `/v1/environments`, `/home`, and
   `/session/{id}` surfaces;
7. an unauthenticated read returns `401`, and a participant credential cannot observe another
   participant or see their private evidence;
8. a pause and resume over HTTP produce a continuation segment that names its predecessor and
   interruption, records stay a paged stream rather than appearing in `status`, and the imported
   trajectory reports collection, execution, termination, and verified outcome independently;
9. snapshot content negotiation streams exactly the rows the in-process export produced; and
10. the canonical `POST /v1/experiments` route accepts a strict `ExperimentSpec`, honours the
    supplied operation ID, and cancellation is idempotent.

When the separately owned decision runtime is selected for the manifest, extend this walkthrough to
exercise its deterministic selector and to verify the optional distribution **without** installing
TypeSafe by default.

Changes to optional adapters, `training.py`, plugin discovery, or dependency bounds also run the
path-routed optional-integration job. Contract paths are separately classified as schema impact;
malformed or incomplete GitHub change metadata fails closed. Before freezing `0.3.0rc1`, verify the
shared compatibility fixtures are required rather than advisory.

The PostgreSQL integration test runs in CI against its configured service. Do not claim a local
PostgreSQL pass when `ENVIRONMENT_HARNESS_POSTGRES_URL` was absent and the test was skipped.

### 5. Review and merge the release pull request after the intended release contents

Use the normal protected-branch process. At least one independent maintainer and every applicable
CODEOWNER must review the complete commit range. Merge all intended code and documentation before
the release pull request. If reviewed work lands after that pull request but before publication, the
publisher requires the version-introducing merge to remain on `main`'s first-parent history and the
coordinated version metadata to remain unchanged. It builds and tags the exact current `main` commit,
so a failed, untagged publication can be retried without consuming another prerelease number.

## Publish the reviewed commit

### 1. Dispatch the reviewed release PR

Run **Attested GitHub release** from GitHub Actions on `main` and enter only the merged release pull
request number. Do not type a version or create a tag locally. The workflow reads the coordinated
version from current `main`, confirms that the reviewed merge introduced it, and derives the tag.

The workflow fails before creating a tag unless the pull request:

- is merged into `main`;
- remains on the current `main` first-parent history;
- introduces a version newer than its first parent;
- introduces the version still present in current coordinated metadata; and
- names a version and tag that do not conflict with a different release commit.

The workflow itself, artifact build, and tag remain bound to the exact current `main` commit. The
pull request number identifies the reviewed origin of that version; it is not used as a checkout ref.

The build checks out the workflow's current `GITHUB_SHA` directly and verifies it as the resolved
release commit. The version-origin commit remains an audit and validation input only. Operator input
and job outputs are never used as executable checkout refs in the privileged publisher.

### 2. Approve publication once

The workflow:

1. resolves the merged release PR and derives its version and tag;
2. rebuilds, validates, and attests the coordinated artifacts before creating a public ref;
3. waits for independent approval in `release-tag`, then creates the immutable tag;
4. creates the immutable GitHub release from the same attested artifacts; and
5. publishes the reviewed Python distributions through PyPI Trusted Publishing.

The single approver must not be the release pull request author or automation initiator. The
branch-restricted `github-release` and `pypi` environments retain their security boundaries without
requiring the same reviewer to approve the same artifact identity again.
Never move, replace, or rebuild artifacts under an existing release tag or PyPI version.

## Verify a published candidate

Confirm that the GitHub release is marked as a prerelease and that PyPI shows the same version.
Then test an installation outside the repository:

```sh
python -m venv /tmp/environment-harness-release-check
/tmp/environment-harness-release-check/bin/python -m pip install \
  "environment-harness[server]==X.Y.ZrcN"
/tmp/environment-harness-release-check/bin/environment-harness doctor
```

Create synthetic data and start the installed viewer if the release changes the SDK, server,
storage, or UI:

```sh
/tmp/environment-harness-release-check/bin/environment-harness \
  --store /tmp/environment-harness-release-check/store quickstart --turns 3
/tmp/environment-harness-release-check/bin/environment-harness \
  --store /tmp/environment-harness-release-check/store serve --open
```

Record defects against the candidate. PyPI does not allow replacing it; prepare and publish a newer
candidate instead.

## Finalize the release

After the candidate is approved, add any final user-facing notes under `Unreleased`, then run
**Prepare release pull request** with bump `none` and stage `final`. The equivalent local command is:

```sh
python scripts/release_version.py prepare --final
```

This removes the prerelease suffix and moves the accumulated notes to a dated final-version
heading. Validate, check, review, merge, tag, approve, and verify the final release through the same
process. A direct stable release remains available with `prepare --bump ...` when staging is not
needed.

## Failure handling

| Failure | Response |
| --- | --- |
| A release check fails before merge | Fix it in the release pull request and rerun all affected checks |
| The upstream Action metadata audit is rate-limited | Authenticate it with the build job's contents-read `GITHUB_TOKEN`; do not skip or weaken the audit |
| The protected tag lookup reports a missing ref | Create the tag only after validating GitHub's `404 Not Found` response; any other lookup failure must stop publication |
| A manually created tag points at the wrong commit | It cannot start publication; leave it immutable and prepare the next candidate |
| Reviewed work lands after the release PR | Retry with the version-introducing PR only while its merge remains on `main`'s first-parent history and current coordinated metadata still names that version |
| GitHub publication fails before creating a release | Fix the workflow or environment and rerun only after confirming the artifact identity contract |
| A published candidate is incorrect | Leave it immutable and publish the next candidate |
| The final version is incorrect on PyPI | Do not overwrite it; follow PyPI incident policy and prepare a new patch release |
| Version files disagree | Rerun or fix `release_version.py`; never publish manually assembled artifacts |

## Documentation and maintenance triggers

Every release pull request must check whether it needs updates to:

- `README.md` for installation or first-use behavior;
- `docs/assets/` when the README's viewer screenshots no longer match the packaged UI;
- [Deployment](DEPLOYMENT.md) for server, authentication, storage, or support boundaries;
- [Viewer maintenance](VIEWER-MAINTENANCE.md) for the browser build or test contract;
- [Protocol](PROTOCOL.md), generated [API reference](API-REFERENCE.md), and `contracts/` for API changes;
- [Compatibility](COMPATIBILITY.md) for upgrade behavior;
- [Authentication](AUTHENTICATION.md) whenever authentication requirements, credential policies, or
  resource constraints change;
- [Decision runtime](DECISION-RUNTIME.md) when the minimum decision payloads or the seam's
  inclusion state change;
- the downstream-impact appendix above for any new breaking change, with its last compatible pin
  and required migration;
- [Release scope](STATUS.md) for newly qualified or explicitly unqualified capabilities; and
- `CHANGELOG.md` for every user-visible change.

Update this guide whenever version rules, artifacts, required checks, approvals, workflow names, or
publisher configuration change.
