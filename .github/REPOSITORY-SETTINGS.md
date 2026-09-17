# Required repository settings

GitHub settings are part of the security boundary and cannot be enforced by files alone. Repository administrators must configure:

- a `main` ruleset requiring the `Python`, `Cross-platform`, `TypeScript`, `Security`, and `Distribution` checks;
- at least one independent approval, required CODEOWNERS approval, dismissal of stale approvals, and approval of the complete commit range;
- blocked direct pushes, force pushes, branch deletion, and ordinary administrator/team bypasses;
- a `v*` tag ruleset allowing creation only by release maintainers, while blocking update/deletion and automation bypass;
- protected `github-release` environment approval by a security/release maintainer other than the release PR author;
- immutable GitHub Releases and GitHub Private Vulnerability Reporting; and
- review of the complete release commit range before a maintainer creates a tag.

Do not install a release GitHub App or store a long-lived release credential in Actions. Release preparation and protected tag creation are deliberate maintainer operations. Dependency updates are never auto-merged. Review branch rules after ownership changes and record exceptions in a tracked security issue.

After adding notes under `Unreleased`, a maintainer creates a release branch and runs `python scripts/release_version.py prepare --bump patch` (or `minor`/`major`). The helper synchronizes every Python, TypeScript, lockfile, and README version. Validate the proposed tag with `python scripts/release_version.py validate --release-tag vX.Y.Z`, run the release checks, and open a normal reviewed PR.

After that PR merges, copy its merge commit SHA from GitHub; do not substitute the newest `main` commit if later changes have landed. Fetch `main` and tags, detach at the recorded merge commit, and run `python scripts/release_version.py detect`. A release maintainer then creates the reported protected tag at that recorded commit with `git tag vX.Y.Z <release-merge-sha>` and pushes only `refs/tags/vX.Y.Z`. The tag dispatches the attested publisher, which still waits for independent `github-release` environment approval.

The publisher requires the tag target, checked-out `HEAD`, and GitHub workflow `GITHUB_SHA` to be the same commit on `main`; its package metadata must equal the tag, and its first parent must carry an older package version. This prevents a later `main` commit from being substituted for the reviewed release merge. Provenance therefore identifies the commit that supplied the released files, and an existing release can only be rerun when every artifact is byte-for-byte identical. Keep the `github-release` environment approval independent of the release PR author and automation initiator.
