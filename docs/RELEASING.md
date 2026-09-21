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

Repository administrators must first configure the protected branch, tag, `github-release`
environment, `pypi` environment, and PyPI Trusted Publisher described in
[Required repository settings](../.github/REPOSITORY-SETTINGS.md). The publisher must target:

- GitHub owner: `kimpton-ai`
- Repository: `environment-harness`
- Workflow: `release.yml`
- Environment: `pypi`
- PyPI project: `environment-harness`

No PyPI password or API token belongs in GitHub Actions.

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

## Prepare a release candidate

### 1. Finish the changelog

Put every user-facing change under `## Unreleased` in `CHANGELOG.md`. Include compatibility or
migration guidance for intentionally incompatible changes.

### 2. Synchronize the release version

For the first candidate on a new release line, choose the semantic version bump:

```sh
python scripts/release_version.py prepare --bump patch --prerelease rc
```

Use `minor` or `major` when appropriate. The command synchronizes:

- `pyproject.toml`;
- `src/environment_harness/__init__.py`;
- `uv.lock`;
- `packages/typescript/package.json` and its lockfile;
- TypeScript artifact examples; and
- `docs/STATUS.md`.

Do not edit those versions separately.

For another candidate on the same release line after adding new `Unreleased` notes:

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

The PostgreSQL integration test runs in CI against its configured service. Do not claim a local
PostgreSQL pass when `ENVIRONMENT_HARNESS_POSTGRES_URL` was absent and the test was skipped.

### 5. Review and merge the release pull request

Use the normal protected-branch process. At least one independent maintainer and every applicable
CODEOWNER must review the complete commit range. Do not create the tag before the release pull
request merges.

## Publish the reviewed commit

### 1. Record the release merge commit

Copy the release pull request's merge commit SHA from GitHub. If later work has landed on `main`, do
not replace it with the newest commit.

### 2. Detect the expected tag at that commit

Fetch `main` and tags, then detach at the recorded merge commit:

```sh
git fetch origin main --tags
git switch --detach RELEASE_MERGE_SHA
python scripts/release_version.py detect
```

The command reports whether a tag should be created and its exact name.

### 3. Create and push only the protected tag

```sh
git tag vX.Y.ZrcN RELEASE_MERGE_SHA
git push origin refs/tags/vX.Y.ZrcN
```

Never move, replace, or rebuild artifacts under an existing release tag or PyPI version.

### 4. Approve publication independently

The tag starts `.github/workflows/release.yml`. The workflow:

1. proves that the tag target, checked-out commit, and workflow SHA are the same commit on `main`;
2. rebuilds and validates the coordinated artifacts;
3. creates build-provenance attestations;
4. waits for independent approval in `github-release`;
5. creates an immutable GitHub release; and
6. waits for independent approval in `pypi` before Trusted Publishing.

The approver must not be the release pull request author or automation initiator.

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

After the candidate is approved, add any final user-facing notes under `Unreleased`, then run:

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
| The tag points at the wrong commit | Do not bypass the workflow; create the correct new version after maintainer review |
| GitHub publication fails before creating a release | Fix the workflow or environment and rerun only after confirming the artifact identity contract |
| A published candidate is incorrect | Leave it immutable and publish the next candidate |
| The final version is incorrect on PyPI | Do not overwrite it; follow PyPI incident policy and prepare a new patch release |
| Version files disagree | Rerun or fix `release_version.py`; never publish manually assembled artifacts |

## Documentation and maintenance triggers

Every release pull request must check whether it needs updates to:

- `README.md` for installation or first-use behavior;
- [Deployment](DEPLOYMENT.md) for server, authentication, storage, or support boundaries;
- [Viewer maintenance](VIEWER-MAINTENANCE.md) for the browser build or test contract;
- [Protocol](PROTOCOL.md), generated [API reference](API-REFERENCE.md), and `contracts/` for API changes;
- [Compatibility](COMPATIBILITY.md) for upgrade behavior;
- [Release scope](STATUS.md) for newly qualified or explicitly unqualified capabilities; and
- `CHANGELOG.md` for every user-visible change.

Update this guide whenever version rules, artifacts, required checks, approvals, workflow names, or
publisher configuration change.
