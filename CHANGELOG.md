# Changelog

## Unreleased

The command line gains `list`, `show` and `timeline`. `timeline` groups recorded events by the state revision an action was taken from and prints one line per participant with the observation, the attempted action and the executed outcome; `--participant`, `--kind` and `--verbose` narrow or expand it. `compare` now prints a readable summary by default, and `--json` on these commands prints the underlying records, so scripts that parsed `compare` output should pass `--json`. Comparison records include `participants` and `revision`.

The browser viewer is read-only. It groups the timeline by revision, names environments by their participants and role, collapses inherited history into one line, renders scores as cards, and uses a sandstone palette that follows the system light or dark preference. Checkpoint, pause, cancel and branch controls moved out of the viewer; they remain command-line and SDK operations.

The README's local startup now uses `serve --open` to open and connect the viewer automatically. A single-use loopback connection ticket avoids copying credentials, and tab-scoped storage keeps the local viewer connected across refreshes. Supplier API authentication remains required.

## 0.1.0

EnvironmentHarness provides persistent shared environments, participant-specific observations, checkpoints, isolated branches and recorded evidence. The local viewer supports inspection, comparison and JSONL export.

The release includes Python and TypeScript clients, an authenticated HTTP service, and runnable synthetic examples. The examples require no account or model API key. Their results demonstrate the SDK workflow and do not measure model intelligence or safety.
