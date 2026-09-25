# Contributing

Use Python 3.12 or later, uv 0.12.0, Node.js 22, and npm 11.17 or later. Run `uv sync --extra server` and `npm ci --ignore-scripts --prefix packages/typescript`. Resolution observes the seven-day dependency cooldown in [docs/DEPENDENCY-SECURITY.md](docs/DEPENDENCY-SECURITY.md).

The PostgreSQL integration test uses PostgreSQL 17.6 Alpine at an immutable container digest in the weekly workflow. Update both the version and digest deliberately when testing a newer database release.

Python and TypeScript ship as one coordinated three-component SemVer release. Keep user-facing changes under `Unreleased`; do not edit package versions individually. Patch releases contain backward-compatible fixes. Minor releases contain new capabilities and, while the SDK is below 1.0, any intentionally incompatible public change with documented migration guidance. After 1.0, incompatible public API or contract changes require a major release.

To release, create a release branch after adding notes under `Unreleased`, then follow [How to release EnvironmentHarness](docs/RELEASING.md). The release helper synchronizes Python, TypeScript, lockfile, documentation and status versions; do not edit package versions individually.

## Branch names

Branches created in this repository follow [Conventional Branch 1.1.0](https://conventionalbranch.org/) using `<type>/<description>`. Prefer `feature/`, `bugfix/`, `hotfix/`, `release/`, or `chore/` and a concise lowercase, kebab-case description; `feat/` and `fix/` are accepted aliases. Name work by its purpose rather than by its author or development tool. For example: `feature/session-progress-stream`, `bugfix/viewer-refresh-route`, or `release/v1.2.0`.

`main` and automated `dependabot/*` branches are exempt. Contributors using forks should follow the same convention when practical, but a pull request is not rejected solely because a fork's source branch uses a different name.

After the release PR merges, copy its merge commit SHA from GitHub; do not use the newest `main` commit if later changes have landed. A release maintainer fetches `main` and tags, detaches at the recorded merge commit, and runs `python scripts/release_version.py detect`. Create the reported protected tag with `git tag vX.Y.Z <release-merge-sha>` and push only `refs/tags/vX.Y.Z`. The tag dispatches the attested publisher, which waits for independent `github-release` environment approval. Release preparation and tag creation intentionally use no stored GitHub App private key or other long-lived Actions credential. Never rebuild different artifacts under an existing version.

## Continuous integration

Pull requests always report the five required checks below. The checks run in parallel against the proposed merge result. The full CI workflow does not run again after merging to `main`; the post-merge workflow records canonical coverage for the merged commit and runs the Windows compatibility shard instead.

| Check | When it runs | What it establishes |
| --- | --- | --- |
| Python | Every pull request and manual CI run | Python tests, PostgreSQL integration, coverage, regression proof, authorization mutation checks, Ruff, Pyright, and contract drift |
| Cross-platform | Every pull request and manual CI run | Aggregated compatibility coverage when the compatibility matrix is required; otherwise a lightweight confirmation that the matrix was intentionally skipped |
| TypeScript | Every pull request and manual CI run | TypeScript compilation, tests, dependency policy, and generated viewer asset drift |
| Security | Every pull request and manual CI run | Dependency review, secret scanning, workflow analysis, package audits, repository policy, and vulnerability scanning |
| Distribution | Every pull request and manual CI run | Python and TypeScript package construction, clean-install smoke tests, browser security, and browser UI behavior |

The `Changes` job decides whether to run the three pull-request compatibility shards: Python 3.13 and 3.14 on Linux, and Python 3.12 on macOS. Manual CI runs always include them, plus Python 3.12 on Windows. On pull requests, they run when a change touches:

- Python source outside `src/environment_harness/viewer/`;
- Python tests, examples, scripts, or generated contracts;
- `pyproject.toml`, `uv.lock`, or `Makefile`; or
- `.github/workflows/ci.yml` itself.

Viewer-only, TypeScript-only, documentation-only, and unrelated workflow-only changes skip those three runners. The required `Cross-platform` check still reports success after verifying that the skip was intentional. A pull request that changes CI routing runs the matrix once to validate the new routing logic.

The classifier evaluates both the current and previous path of renamed files. It also compares the number of files returned by GitHub with the pull request's authoritative changed-file count. Missing, truncated, or malformed file data fails closed by running the compatibility matrix. The security team owns changes to the CI workflow, and the `main` ruleset accepts required status checks only from the GitHub Actions integration.

| Example change | Compatibility matrix | Required checks that validate it |
| --- | --- | --- |
| Python SDK, server, storage, or runtime | Runs | All five |
| Viewer HTML, CSS, or checked-in JavaScript only | Skips | Distribution exercises the browser; the other required checks retain the integrated release baseline |
| TypeScript client only | Skips | TypeScript, Security, and Distribution provide the relevant package checks; all required checks still report |
| Documentation or `coverage.yml` only | Skips | Required checks still report; Cross-platform remains a lightweight gate |
| Python dependency or CI workflow | Runs | All five, including every supported Python and pull-request operating-system shard |

Pushes to `main` run `.github/workflows/coverage.yml`, which produces `coverage.json` for the repository ratchet and `coverage.xml` for Codecov, and runs the Windows compatibility shard. The weekly workflow performs extended dependency and compatibility assurance. Version tags alone start the attested release workflow.

Windows runs after merge rather than on every pull request because it is roughly four times the slowest Linux shard even in parallel, and it was the sole reason a pull request waited about seven minutes for `Cross-platform`. It is the only runner that reaches the `os.name == "nt"` branches in `adapters/_subprocess.py` and `store.py`, so a Windows regression surfaces on the merge that introduced it rather than not at all. Run it before merging with a manual CI dispatch, which always includes every shard.

Before submitting a change, run `make check`, `make viewer`, `npm run typecheck --prefix packages/typescript`, and `make build`. Include a small reproducible example of the behavior you changed. Schema changes must update the generated JSON and TypeScript contracts. `src/environment_harness/presentation.py` and `packages/typescript/src/timeline.ts` must keep the same turn-grouping rules and field names so the command line and the viewer describe evidence identically. Do not change protocol semantics without describing compatibility and migration.

Viewer changes must follow the [viewer style guide](docs/STYLE-GUIDE.md) and [viewer maintenance guide](docs/VIEWER-MAINTENANCE.md). Deployment-facing changes must update the [deployment guide](docs/DEPLOYMENT.md) in the same pull request.

CI requires at least 90% statement and 80% branch coverage overall and for security-critical modules. Changed executable lines require 100% diff coverage. A changed regression test must also fail when applied to `origin/main`.

Keep domain implementations, credentials and recorded private data out of this repository. Examples and test fixtures must be synthetic or explicitly redistributable. Optional integration support should state its actual checkpoint and recovery limits.

Open a GitHub issue for a bug or integration question. Include the release version, operating system, exact command and a sanitized reproduction. Never attach credentials or a private environment store. Report security concerns privately as described in [SECURITY.md](SECURITY.md).
