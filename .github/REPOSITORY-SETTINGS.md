# Required repository settings

GitHub settings are part of the security boundary and cannot be enforced by files alone. Repository administrators must configure:

- a `main` ruleset requiring the `Python`, `Cross-platform`, `TypeScript`, `Security`, and `Distribution` checks;
- at least one independent approval, required CODEOWNERS approval, dismissal of stale approvals, and approval of the complete commit range;
- blocked direct pushes, force pushes, branch deletion, and ordinary administrator/team bypasses;
- a `v*` tag ruleset allowing creation only by release maintainers and blocking update/deletion;
- protected `github-release` environment approval by the security/release team;
- immutable GitHub Releases and GitHub Private Vulnerability Reporting; and
- GitHub App identity verification for automation—names resembling trusted bots are not sufficient.

Dependency updates are never auto-merged. Review branch rules after ownership or GitHub App changes and record exceptions in a tracked security issue.

Dispatch the release workflow from the protected release tag and supply that same tag as its input. The workflow requires the tag target, checked-out `HEAD`, and GitHub workflow `GITHUB_SHA` to be the same commit on `main`; its package metadata must equal the tag. Provenance therefore identifies the commit that supplied the released files, and an existing release can only be rerun when every artifact is byte-for-byte identical.
