This is the standalone public EnvironmentHarness SDK. Keep private suppliers, marketplace code, deployment state, credentials and recorded launch videos outside this repository. Use EnvironmentHarness, EnvironmentSession and environment sessions consistently in code, API and documentation. Run Python tests, Ruff, TypeScript type checking/build, schema drift and distribution checks before release. Synthetic examples demonstrate SDK behavior only.

Read `docs/DEPLOYMENT.md` before changing server startup, authentication, storage or support boundaries. Read `docs/VIEWER-MAINTENANCE.md` before changing the browser UI or its generated assets. Read `docs/RELEASING.md` before changing versions, artifacts, checks or publishing workflows. Update the affected guide in the same change.

## Branch names

Use the project policy derived from [Conventional Branch 1.1.0](https://conventionalbranch.org/):

- Name repository branches `<type>/<description>`.
- Prefer the full, purpose-based types `feature/`, `bugfix/`, `hotfix/`, `release/`, and `chore/`. The `feat/` and `fix/` aliases are accepted. Although the upstream specification also recognizes agent names such as `codex/` and `claude/`, this project names ordinary work by purpose rather than by the tool that produced it.
- Write a concise, lowercase, kebab-case description, normally two to five words. Use only letters, digits, and single hyphens. Do not use spaces, underscores, consecutive separators, or leading or trailing separators. Dots are permitted only for version numbers under `release/`.
- Include an issue identifier when one already exists; do not invent or request one solely to name a branch.
- `main` is the project's exempt trunk branch. Do not create alternate trunk branches.
- `dependabot/*` is an explicit automation exception. External contributors should follow this policy, but a pull request from a fork must not be rejected solely because its source branch has a different name.
- Do not rename an existing branch solely to enforce this policy unless the user explicitly requests the rename.

Examples: `feature/session-progress-stream`, `bugfix/viewer-refresh-route`, `chore/update-release-docs`, and `release/v1.2.0`.
