"""Issue bearer credentials for tests through the server-owned policy API.

Tests never assert permissions: they name a policy and let the store persist it.
"""

from environment_harness.access import _AccessContext


def bearer(store, access: _AccessContext, *, ttl: int = 3600) -> str:
    if access.policy == "participant":
        return store.issue_participant(
            access.tenant,
            access.subject,
            session=access.session,
            participant=access.participant,
            generation=access.generation,
            ttl=ttl,
        )
    if access.policy == "viewer":
        return store.issue_viewer(access.tenant, access.subject, ttl=ttl)
    return store.issue_admin(access.tenant, access.subject, ttl=ttl)


def management(tenant="tenant", subject="admin") -> _AccessContext:
    return _AccessContext(tenant=tenant, subject=subject, policy="admin")


def viewer(tenant="tenant", subject="loopback-viewer") -> _AccessContext:
    return _AccessContext(tenant=tenant, subject=subject, policy="viewer")
