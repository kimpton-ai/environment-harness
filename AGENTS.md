This is the standalone public EnvironmentHarness SDK. Keep private suppliers, marketplace code, deployment state, credentials and recorded launch videos outside this repository. Use EnvironmentHarness, EnvironmentSession and environment sessions consistently in code, API and documentation. Run Python tests, Ruff, TypeScript type checking/build, schema drift and distribution checks before release. Synthetic examples demonstrate SDK behavior only.

Read `docs/DEPLOYMENT.md` before changing server startup, authentication, storage or support boundaries. Read `docs/VIEWER-MAINTENANCE.md` before changing the browser UI or its generated assets. Read `docs/RELEASING.md` before changing versions, artifacts, checks or publishing workflows. Update the affected guide in the same change.

## Branch names

Follow [Conventional Branch](https://conventionalbranch.org/) using `<type>/<description>`. Prefer purpose-based types: `feature/` (or `feat/`), `bugfix/` (or `fix/`), `hotfix/`, `release/`, and `chore/`. Use lowercase alphanumeric words separated by single hyphens; dots are permitted only for version numbers in release branches. Do not use spaces, underscores, consecutive separators, or leading or trailing separators. Keep the description concise and include an issue number when one exists. Trunk branches such as `main` are exempt.
