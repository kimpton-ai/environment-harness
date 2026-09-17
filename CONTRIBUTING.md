# Contributing

Use Python 3.12 or later, uv and Node.js 22 or later. Run `uv sync --extra server` and `npm ci --prefix packages/typescript`.

Before submitting a change, run `make check`, `make viewer`, `npm run typecheck --prefix packages/typescript`, and `make build`. Include a small reproducible example of the behavior you changed. Schema changes must update the generated JSON and TypeScript contracts. `src/environment_harness/presentation.py` and `packages/typescript/src/timeline.ts` must keep the same turn-grouping rules and field names so the command line and the viewer describe evidence identically. Do not change protocol semantics without describing compatibility and migration.

Keep domain implementations, credentials and recorded private data out of this repository. Examples and test fixtures must be synthetic or explicitly redistributable. Optional integration support should state its actual checkpoint and recovery limits.

Open a GitHub issue for a bug or integration question. Include the release version, operating system, exact command and a sanitized reproduction. Never attach credentials or a private environment store. Report security concerns privately as described in [SECURITY.md](SECURITY.md).
