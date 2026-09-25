# Required repository settings

GitHub settings are part of the security boundary and cannot be enforced by files alone. Repository administrators must configure:

- a `main` ruleset requiring the `Python`, `Cross-platform`, `TypeScript`, `Security`, and `Distribution` checks, each bound to the GitHub Actions integration as its expected source;
- at least one independent approval, required CODEOWNERS approval, dismissal of stale approvals, and approval of the complete commit range;
- blocked direct pushes, force pushes, branch deletion, and ordinary administrator/team bypasses;
- a `v*` tag ruleset blocking every update and deletion, with no bypasses; tag creation must remain
  available to the protected publisher's short-lived built-in `GITHUB_TOKEN`;
- a protected `release-tag` environment requiring approval by a security/release maintainer other
  than the release PR author or workflow initiator, with administrator bypass disabled;
- branch-restricted `github-release` and `pypi` environments with administrator bypass disabled,
  but no additional required reviewers after the `release-tag` approval;
- GitHub Actions restricted to GitHub-owned actions and the explicitly approved third-party actions already used by the workflows, with full commit-SHA pinning required;
- Dependabot alerts and security updates, immutable GitHub Releases, and GitHub Private Vulnerability Reporting; and
- permission for the CODEOWNER-protected `release-prepare.yml` workflow to create branches and pull
  requests with `GITHUB_TOKEN`; it must never approve or merge its own pull request; and
- review of the complete release commit range before the protected publisher creates a tag.

Do not install a release GitHub App or store a long-lived release credential in Actions. Release
preparation and protected tag creation use the short-lived workflow `GITHUB_TOKEN`, with write
permissions isolated to their dedicated jobs. Do not restrict tag creation: the built-in Actions
app cannot use a `github-actions[bot]` user bypass. A tag push never starts publishing, and the
workflow still verifies the reviewed version, current commit, approval, attestation, and exact tag
target. Treat an unexpected version tag as a security incident. Dependabot may open security-update
pull requests, but dependency updates are never auto-merged. Review branch rules and the Actions
allowlist after ownership or workflow changes, and record exceptions in a tracked security issue.

Treat CI path classification only as a runner-cost optimization. Required checks must continue to report on every pull request, and incomplete GitHub file metadata must run additional checks rather than skip them. The security team owns `.github/workflows/ci.yml`; maintain the required CODEOWNER and last-push approval rules so proposed workflow code cannot approve itself.

Release immutability applies when a release is published after the repository setting is enabled. A legacy mutable release cannot be locked retroactively through GitHub's release settings; replacing one requires a separately reviewed migration because it changes public release metadata and artifact availability.

Before the first publication, create a pending Trusted Publisher on production PyPI for project `environment-harness`. Create it within the Kimpton PyPI organization when that organization is ready. Use GitHub owner `kimpton-ai`, repository `environment-harness`, workflow filename `release.yml`, and environment name `pypi`. The pending publisher creates the project on its first successful publication, so do not create an API token or add a PyPI password to GitHub.

After adding notes under `Unreleased`, run **Prepare release pull request** on `main`. Choose a
`patch`, `minor`, or `major` bump and the `rc` stage for the first candidate. The workflow creates a
PEP 440 Python version such as `0.2.3rc1`, the corresponding npm SemVer version `0.2.3-rc.1`, a
`release/v0.2.3rc1` branch, and a normal reviewed pull request. For another candidate, choose bump
`none` and stage `rc`. Alpha and beta stages are also available; stages may advance but never move
backwards. The underlying `release_version.py prepare` commands remain available for local
validation and recovery.

When the release candidate is approved, run **Prepare release pull request** with bump `none` and
stage `final`. The finalization PR removes the prerelease suffix and moves accumulated `Unreleased`
notes under the final `X.Y.Z` changelog heading. A direct stable release remains available by
choosing a semantic bump and stage `stable` when staging is unnecessary.

Merge the release PR only after every intended code and documentation change. Then run **Attested
GitHub release** on `main` and enter the merged release PR number. The workflow requires that PR to
have introduced the coordinated version still present on current `main`, derives the tag without
operator input, builds and attests the current commit's artifacts, and pauses for independent
`release-tag` approval before creating the tag. The approved workflow then creates the GitHub
release and publishes to PyPI without repeating the same manual approval. PyPI release candidates
are real immutable releases, but ordinary `pip install environment-harness` excludes them; testers
should request the exact RC or use `--pre`.

The publisher requires the version-origin PR merge to remain on current `main`'s first-parent
history and to introduce a version newer than its parent. Checked-out `HEAD`, workflow `GITHUB_SHA`,
the built artifacts, and the tag target must all be the same current commit, whose coordinated
metadata must still name that version. Provenance therefore identifies the commit that supplied the
released files, and reruns accept an existing tag only when it still targets that exact commit. PyPI
never permits replacing a published version, so a published or incorrect candidate must be followed
by a newer candidate. Keep the `release-tag` approval independent of the release PR author and
automation initiator.
