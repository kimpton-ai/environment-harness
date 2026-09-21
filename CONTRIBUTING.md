# Contributing

Use Python 3.12 or later, uv 0.12.0, Node.js 22, and npm 11.17 or later. Run `uv sync --extra server` and `npm ci --ignore-scripts --prefix packages/typescript`. Resolution observes the seven-day dependency cooldown in [docs/DEPENDENCY-SECURITY.md](docs/DEPENDENCY-SECURITY.md).

The PostgreSQL integration test uses PostgreSQL 17.6 Alpine at an immutable container digest in the weekly workflow. Update both the version and digest deliberately when testing a newer database release.

Python and TypeScript ship as one coordinated three-component SemVer release. Keep user-facing changes under `Unreleased`; do not edit package versions individually. Patch releases contain backward-compatible fixes. Minor releases contain new capabilities and, while the SDK is below 1.0, any intentionally incompatible public change with documented migration guidance. After 1.0, incompatible public API or contract changes require a major release.

To release, create a release branch after adding notes under `Unreleased`, then run `python scripts/release_version.py prepare --bump patch` (or `minor`/`major`). It moves the accumulated notes to a dated version and synchronizes `pyproject.toml`, `environment_harness.__version__`, `uv.lock`, the TypeScript package and lockfile, the TypeScript artifact example, and the release status. Validate the proposed tag with `python scripts/release_version.py validate --release-tag vX.Y.Z`, run the release checks, and merge the release PR normally.

After the release PR merges, copy its merge commit SHA from GitHub; do not use the newest `main` commit if later changes have landed. A release maintainer fetches `main` and tags, detaches at the recorded merge commit, and runs `python scripts/release_version.py detect`. Create the reported protected tag with `git tag vX.Y.Z <release-merge-sha>` and push only `refs/tags/vX.Y.Z`. The tag dispatches the attested publisher, which waits for independent `github-release` environment approval. Release preparation and tag creation intentionally use no stored GitHub App private key or other long-lived Actions credential. Never rebuild different artifacts under an existing version.

Before submitting a change, run `make check`, `make viewer`, `npm run typecheck --prefix packages/typescript`, and `make build`. Include a small reproducible example of the behavior you changed. Schema changes must update the generated JSON and TypeScript contracts. `src/environment_harness/presentation.py` and `packages/typescript/src/timeline.ts` must keep the same turn-grouping rules and field names so the command line and the viewer describe evidence identically. Do not change protocol semantics without describing compatibility and migration.

Viewer changes must follow the [viewer style guide](docs/STYLE-GUIDE.md).

CI requires at least 90% statement and 80% branch coverage overall and for security-critical modules. Changed executable lines require 100% diff coverage. A changed regression test must also fail when applied to `origin/main`.

Keep domain implementations, credentials and recorded private data out of this repository. Examples and test fixtures must be synthetic or explicitly redistributable. Optional integration support should state its actual checkpoint and recovery limits.

Open a GitHub issue for a bug or integration question. Include the release version, operating system, exact command and a sanitized reproduction. Never attach credentials or a private environment store. Report security concerns privately as described in [SECURITY.md](SECURITY.md).
