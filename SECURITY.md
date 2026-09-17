# Security policy

## Supported versions

Only the latest immutable GitHub release is supported. The `main` branch is unreleased development. Older releases require upgrading unless a maintainer explicitly announces extended support.

## Report a vulnerability privately

Use [GitHub Private Vulnerability Reporting](https://github.com/kimpton-ai/environment-harness/security/advisories/new) as the primary reporting path. If that is unavailable, email support@kimpton.ai with the subject `EnvironmentHarness security`.

Include:

- the affected release or commit and the expected impact;
- a minimal synthetic reproduction and relevant sanitized configuration;
- the suspected attack path and affected security boundary; and
- whether the issue or exploit has been disclosed elsewhere.

Do not send live credentials, private trajectories, production environment stores, supplier code, deployment state, or an unnecessary working exploit.

Maintainers aim to acknowledge a report within five business days. This is a target, not a contractual SLA or bug bounty. The security team will validate scope, identify affected versions, assign a primary owner, coordinate remediation and disclosure, publish an advisory, and credit the reporter if desired.

Please wait for an agreed disclosure date. Fixes and exploit details remain private until patched artifacts and the advisory are ready. The advisory will identify affected and fixed versions, mitigations, CVE/GHSA identifiers when applicable, and any required credential rotation.

## Security boundaries

EnvironmentHarness enforces authenticated HTTP principals, tenant/environment/participant scope, generation checks, and evidence filtering. The researcher viewer is not an authorization boundary.

The following remain trusted:

- Python callers with local access to an `EnvironmentSession` or its store;
- installed plugins and database owners; and
- command subprocesses launched without a separate operating-system isolation boundary.

Use an isolated backend for hostile programs. The deployer or private deployment repository owns production TLS, reverse proxies, cloud IAM, secrets, backups, logging, network policy, and request-rate controls. Keep private suppliers, marketplace logic, credentials, deployment state, production environment sessions, and recorded launches outside this public SDK.

## Dependency and CI incidents

Dependency releases are held for 168 hours before adoption, including security updates, unless an exact tracked exception is approved by a security CODEOWNER. Vulnerability and malware scanning and routine dependency alerts are immediate; they are distinct from private vulnerability reports. See [the dependency and supply-chain policy](docs/DEPENDENCY-SECURITY.md).

If untrusted workflow code could access a credential or writable identity:

1. Revoke the affected credentials first.
2. Inspect at least 30 days of workflow runs and the complete commit ranges.
3. Delete potentially poisoned caches and artifacts.
4. Rotate every credential reachable by those jobs.

Prefer job-scoped OIDC credentials lasting minutes. Exceptional static release credentials must expire within 24 hours.
