# Changelog

## Unreleased

The SDK adds opt-in `environment-session.v2` transitions with bounded operation plans,
host-authorized execution through the existing Operations journal, stable receipts, dependency
ordering, and conservative recovery after unknown outcomes. Remote suppliers use the separate
`environment-worker.v2` contract. Existing v1 environments and worker routes remain supported.

Typed scenarios now expand into bounded concurrent experiment trials with durable
status, deterministic seeds and resumable activity feeds. The evidence viewer adds
experiment grouping, filtering, unequal-length comparison, progression and report
history. The HTTP service now checks in generated OpenAPI, validates command and
operation request shapes, and returns one traceable error envelope across routes and
both SDK clients.

The viewer now expands experiment rows directly into environment sessions instead of adding a
label-only scenario layer. Session rows show scenario and trial identity, status, turn progress,
participants and latest activity; experiment names open stable detail routes. Activity pages and
snapshots now have checked-in JSON Schemas and concrete OpenAPI response models.
Experiment detail routes now separate shared frozen configuration, clickable scenario snapshots,
and environment sessions. Empty/default scenarios do not add navigation; meaningful scenarios
show structured input, reference, metadata, progress, and a filtered path to their sessions.

Environment packages can now expose typed `EnvironmentOperation` classes for
engine-specific or imperative capabilities. Environment and experiment manifests
freeze portable `OperationSpec` identities and JSON configuration, while runtime
clients, credentials and process-local handles remain on the environment instance.
Specialized motor APIs have been removed from the prerelease SDK; integrations use
the environment-owned operation seam without adding domain-specific types to core.

`EnvironmentHarness` now accepts an optional Python `session_runner`, allowing grouped experiments
to interleave ordinary turns with environment operations without replacing scheduling or durable
status. A runnable custom-environment experiment records operation receipts, comparable scores and
evidence-linked findings, and the CLI/browser timelines describe operation activity directly.
The public `SessionRunner` protocol documents this extension boundary. Experiment startup now
validates environment operations before queueing work, runner results must identify the current
environment-session record, and conformance reports how many operation classes it checked.
An external-simulator example keeps the domain client and operation outside core while exercising
a real subprocess connection, explicit external-write policy, idempotent receipts, scoring,
findings and the same multi-session viewer workflow.

Native implementations can run behind authenticated HTTP workers while a trusted
supervisor retains the evidence store. External participants can advance ready
phases through the fenced coordinator. A versioned legacy adapter preserves old
records, and an ORS HTTP/SSE client retains original task receipts without implicit
write retries. See [remote workers](docs/REMOTE-WORKERS.md).

Hosted PostgreSQL/S3 storage adds aggregate environment payload admission and
explicit tenant erasure with confirmed object deletion. Erasure includes experiment,
scenario, environment-session and activity metadata introduced by this release.

Production PyPI publishing now uses short-lived Trusted Publishing credentials and publishes only the reviewed Python wheel and source distribution. Releases support PEP 440 alpha, beta, and release-candidate versions while mapping those versions to npm-compatible SemVer prereleases. Package metadata now includes the rendered README, classifiers, project links, and the `py.typed` marker.

Deployment, release, and viewer-maintenance guides now define the supported local topology, hosted-service qualification boundary, prerelease workflow, generated browser assets, required checks, and documentation triggers for future contributors.

## 0.2.2 - 2026-09-18

Release publishing now extracts immutable-ID artifacts directly into the verified distribution directory before checking attestations and explicitly disables dependency caching during the tag build. Routine Dependabot version updates are grouped into one monthly pull request per ecosystem, while security updates remain immediate.

## 0.2.1 - 2026-09-17

The public SDK now includes coordinated vulnerability reporting, governance and support files, dependency cooldowns, immutable workflow and container pins, credential-free security scans, cross-platform CI, positive distribution manifests, installed-wheel smoke tests, SBOM and tag-triggered artifact-attestation release automation, and protected-release setup guidance. Release maintainers create the reviewed protected tag manually; Actions stores no long-lived release credential. Python and TypeScript coverage gates require at least 90% statements/lines and 80% branches; security-critical Python modules meet those floors independently.

Session recovery now rejects stale worker completion and protects reconciled responses. Cancellation works while a writer is active, terminates managed command process groups and reports unresolved effects. The runner validates registered agent implementations, and conformance supports coordinator closure and explicit event inputs.

Comparisons keep incompatible metric definitions and units in separate groups. Legacy reports remain readable as raw values. Large inherited records use bounded chunks and can be branched repeatedly without adding encoding layers. Existing stores require no migration. See [compatibility and protocol details](docs/COMPATIBILITY.md).

The command line gains `list`, `show` and `timeline`. `timeline` groups recorded events by the state revision an action was taken from and prints one line per participant with the observation, the attempted action and the executed outcome; `--participant`, `--kind` and `--verbose` narrow or expand it. `compare` now prints a readable summary by default, and `--json` on these commands prints the underlying records, so scripts that parsed `compare` output should pass `--json`. Comparison records include `participants` and `revision`.

The browser viewer is read-only. It groups the timeline by revision, names environments by their participants and role, collapses inherited history into one line, renders scores as cards, and uses a sandstone palette that follows the system light or dark preference. Checkpoint, pause, cancel and branch controls moved out of the viewer; they remain command-line and SDK operations.

The README's local startup now uses `serve --open` to open and connect the viewer automatically. A single-use loopback connection ticket avoids copying credentials, and tab-scoped storage keeps the local viewer connected across refreshes. Supplier API authentication remains required.

## 0.1.0

EnvironmentHarness provides persistent shared environments, participant-specific observations, checkpoints, isolated branches and recorded evidence. The local viewer supports inspection, comparison and JSONL export.

The release includes Python and TypeScript clients, an authenticated HTTP service, and runnable synthetic examples. The examples require no account or model API key. Their results demonstrate the SDK workflow and do not measure model intelligence or safety.
