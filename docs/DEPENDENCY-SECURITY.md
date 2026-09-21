# Dependency and supply-chain policy

EnvironmentHarness delays adoption of every new Python, npm, and GitHub Actions release until 168 hours after publication. Scanning and security alerts remain immediate. Existing unchanged lockfile entries are grandfathered until they change.

Python resolution uses uv's one-week `exclude-newer` policy from `uv.toml`. npm resolution uses a seven-day minimum release age and disables lifecycle scripts. CI installs reviewed lockfiles only and independently compares changed versions with their registry publication timestamps. Git, URL, arbitrary-registry, and unpinned GitHub Action dependencies are prohibited.

Dependabot groups routine version updates into one monthly pull request per ecosystem, limiting routine review noise to at most three open pull requests. Security scanning and security update pull requests remain immediate and are not grouped with routine updates. Dependency changes are never auto-merged. A young release cannot merge unless `.github/dependency-exceptions.json` contains an exact, unexpired exception with:

- a tracked issue naming the package, version, advisory, and operational impact;
- an explanation of why waiting is riskier;
- an accountable owner, a review timestamp, and approval by `@kimpton-ai/security`;
- expiry no later than seven days after publication; and
- links to substantive provenance, checksum, signature/attestation, vulnerability, malware, maintainer/repository, and install-script evidence.

CI independently validates approved registries and lockfile hashes, runs pip and npm vulnerability audits, and scans the lockfiles with OSV. OSV includes the independently maintained OpenSSF Malicious Packages feed, so malicious-package screening runs on every pull request and on the weekly schedule; an exception cannot suppress those scans.

Changing the package/version, evidence, or scope requires a fresh security CODEOWNER approval. Repository branch protection must require CODEOWNER review, dismiss stale approvals after new commits, and prevent approval bypass. Secret-scan exceptions are exact entries in `.github/secret-exceptions.json`; each requires the detector, file, line, fingerprint, owner, reason, security approval, and an expiry within 90 days. Broad baseline files are prohibited. See [SECURITY.md](../SECURITY.md) for private reporting and incident response.
