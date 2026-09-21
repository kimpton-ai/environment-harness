# Required repository settings

GitHub settings are part of the security boundary and cannot be enforced by files alone. Repository administrators must configure:

- a `main` ruleset requiring the `Python`, `Cross-platform`, `TypeScript`, `Security`, and `Distribution` checks;
- at least one independent approval, required CODEOWNERS approval, dismissal of stale approvals, and approval of the complete commit range;
- blocked direct pushes, force pushes, branch deletion, and ordinary administrator/team bypasses;
- a `v*` tag ruleset allowing creation only by release maintainers, while blocking update/deletion and automation bypass;
- protected `github-release` environment approval by a security/release maintainer other than the release PR author;
- a protected `pypi` environment with the same independent-review requirement;
- immutable GitHub Releases and GitHub Private Vulnerability Reporting; and
- review of the complete release commit range before a maintainer creates a tag.

Do not install a release GitHub App or store a long-lived release credential in Actions. Release preparation and protected tag creation are deliberate maintainer operations. Dependency updates are never auto-merged. Review branch rules after ownership changes and record exceptions in a tracked security issue.

Before the first publication, create a pending Trusted Publisher on production PyPI for project `environment-harness`. Create it within the Kimpton PyPI organization when that organization is ready. Use GitHub owner `kimpton-ai`, repository `environment-harness`, workflow filename `release.yml`, and environment name `pypi`. The pending publisher creates the project on its first successful publication, so do not create an API token or add a PyPI password to GitHub.

After adding notes under `Unreleased`, start a release candidate with `python scripts/release_version.py prepare --bump patch --prerelease rc` (or `minor`/`major`). This creates a PEP 440 Python version such as `0.2.3rc1` and the corresponding npm SemVer version `0.2.3-rc.1`. The helper synchronizes every Python, TypeScript, lockfile, and README version. Validate the proposed tag with `python scripts/release_version.py validate --release-tag vX.Y.Zrc1`, run the release checks, and open a normal reviewed PR. After an RC is published and more changes have accumulated under `Unreleased`, prepare the next one with `python scripts/release_version.py prepare --prerelease rc`. Alpha and beta stages are also available through `--prerelease alpha` and `--prerelease beta`; stages may advance but never move backwards.

When the release candidate is approved, run `python scripts/release_version.py prepare --final`. The finalization PR removes the prerelease suffix and moves the accumulated `Unreleased` notes under the final `X.Y.Z` changelog heading. A direct stable release remains available with `python scripts/release_version.py prepare --bump patch` when staging is unnecessary.

After each release PR merges, copy its merge commit SHA from GitHub; do not substitute the newest `main` commit if later changes have landed. Fetch `main` and tags, detach at the recorded merge commit, and run `python scripts/release_version.py detect`. A release maintainer then creates the reported protected tag at that recorded commit with `git tag <reported-tag> <release-merge-sha>` and pushes only that tag ref. The tag dispatches the attested publisher, which waits for independent `github-release` approval before creating the GitHub release and then for `pypi` approval before publishing the wheel and source distribution. PyPI release candidates are real immutable releases, but ordinary `pip install environment-harness` excludes them; testers should request the exact RC or use `--pre`.

The publisher requires the tag target, checked-out `HEAD`, and GitHub workflow `GITHUB_SHA` to be the same commit on `main`; its package metadata must equal the tag, and its first parent must carry an older package version. This prevents a later `main` commit from being substituted for the reviewed release merge. Provenance therefore identifies the commit that supplied the released files, and GitHub release reruns only succeed when every artifact is byte-for-byte identical. PyPI never permits replacing a published version, so a failed or incorrect RC must be followed by a newer RC. Keep both environment approvals independent of the release PR author and automation initiator.
