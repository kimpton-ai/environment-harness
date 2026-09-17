# Changelog

## Unreleased

The README's local startup now uses `serve --open` to open and connect the viewer automatically. A single-use loopback connection ticket avoids copying credentials, and tab-scoped storage keeps the local viewer connected across refreshes. Supplier API authentication remains required.

## 0.1.0

EnvironmentHarness provides persistent shared environments, participant-specific observations, checkpoints, isolated branches and recorded evidence. The local viewer supports inspection, comparison and JSONL export.

The release includes Python and TypeScript clients, an authenticated HTTP service, and runnable synthetic examples. The examples require no account or model API key. Their results demonstrate the SDK workflow and do not measure model intelligence or safety.
