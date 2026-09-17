# Required repository settings

GitHub settings are part of the security boundary and cannot be enforced by files alone. Repository administrators must configure:

- a `main` ruleset requiring the `Python`, `Cross-platform`, `TypeScript`, `Security`, and `Distribution` checks;
- at least one independent approval, required CODEOWNERS approval, dismissal of stale approvals, and approval of the complete commit range;
- blocked direct pushes, force pushes, branch deletion, and ordinary administrator/team bypasses;
- a `v*` tag ruleset allowing creation only by release maintainers and the `kimpton-ci` GitHub App, while blocking update/deletion;
- protected `github-release` environment approval by the security/release team;
- immutable GitHub Releases and GitHub Private Vulnerability Reporting; and
- GitHub App identity verification for automation—names resembling trusted bots are not sufficient.

Install `kimpton-ci` for this repository with only Contents and Pull requests write access. Store one of its private keys as the `RELEASE_APP_PRIVATE_KEY` Actions secret and rotate it through the GitHub App settings. The public App ID is pinned in the workflows. Enterprise policy intentionally prevents the ambient `GITHUB_TOKEN` from opening release PRs or bypassing tag creation.

Dependency updates are never auto-merged. Review branch rules after ownership or GitHub App changes and record exceptions in a tracked security issue.

Run **Prepare release PR** with a patch, minor, or major choice after adding notes under `Unreleased`. The workflow synchronizes every Python, TypeScript, lockfile, and README version and opens a normal reviewed PR. When that PR merges, release orchestration creates the protected strict-SemVer tag at the reviewed `main` commit and dispatches the attested publisher on that tag. A maintainer may still push the same protected tag as a recovery path.

The publisher requires the tag target, checked-out `HEAD`, and GitHub workflow `GITHUB_SHA` to be the same commit on `main`; its package metadata must equal the tag. Provenance therefore identifies the commit that supplied the released files, and an existing release can only be rerun when every artifact is byte-for-byte identical. Keep the `github-release` environment approval independent of the release PR author and automation initiator.
