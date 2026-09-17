## Summary

Describe the user-visible change and its compatibility impact.

## Verification

- [ ] Python tests, Ruff format/lint, and Pyright pass.
- [ ] TypeScript type checking, build, and tests pass when applicable.
- [ ] Contracts, viewer assets, distributions, and licenses pass drift checks.
- [ ] Changed executable lines are covered by tests.

## Security and supply chain

- [ ] No credential, private supplier, marketplace, deployment, or recorded-session data is included.
- [ ] Security reports follow the private process in [SECURITY.md](../SECURITY.md).
- [ ] Authentication, authorization, tenant/participant isolation, and error behavior were reviewed where applicable.
- [ ] Dependency and Action changes are lockfile-pinned and at least 168 hours old.
- [ ] Any cooldown or secret exception is exact, tracked, approved, and unexpired.
- [ ] Workflow jobs use least privilege and do not expose secrets to untrusted code.
- [ ] Generated and packaged files contain only the reviewed public manifests.
