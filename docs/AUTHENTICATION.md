# Authentication and authorization

EnvironmentHarness implements **no users, groups, organizations, OAuth, or
RBAC**. It authenticates opaque bearer credentials and resolves each one to an
identity plus a server-owned access policy. An embedding product such as a
hosted control plane authenticates its own users and uses the trusted
administrative seam described below to issue appropriately constrained
EnvironmentHarness credentials.

## Why the four-role model was removed

Before `0.3.0rc1`, `Principal.role` was a public `Literal` with the values
`researcher`, `agent`, `scorer`, and `worker`. SDK callers constructed that
value, examples printed it, requests carried it, and OpenAPI published it as
`x-roles`.

That model was wrong in three ways:

1. **They were never user roles.** `agent` described a participant transport,
   `scorer` described provenance inside a `ScoreReport`, and `worker` described
   a separate process with its own shared secret. Publishing them as a caller
   taxonomy invited consumers to model identity around them.
2. **Callers asserted their own authority.** A caller chose the role that its
   credential would carry. Authorization belongs to the server.
3. **It leaked into every surface.** Removing it later would have been a
   breaking change across the SDK, HTTP API, generated clients, examples, and
   documentation at once.

`Principal` and `Principal.role` are gone. They are not replaced by `kind`,
`x-principal-kinds`, or another custom caller taxonomy. OpenAPI publishes only
its standard HTTP bearer security scheme.

## The bearer-credential contract

A remote client sends exactly one thing:

```http
GET /v1/sessions HTTP/1.1
Authorization: Bearer <opaque-credential>
```

The credential is an opaque random string. The server stores its SHA-256 hash
beside the policy and resource constraints it was issued with; it never trusts
a policy, permission, role, or scope supplied in a request body, query
parameter, or header. Adding a field such as `{"policy": "management"}` to a
request cannot widen access: unknown fields are rejected by the strict command
models, and the persisted policy is the only input to the authorization check.

## The three server-owned credential policies

These names describe internal credential behavior. They are not users,
organization membership, or public roles.

| Policy | Issued by | May do |
| --- | --- | --- |
| `management` | `environment-harness token`, or `EnvironmentHarness.management_credential()` in an embedding process | The authenticated management surface: create experiments and sessions, run lifecycle commands, read full evidence, register and ingest sources, freeze snapshots and datasets, read recorded training results, and issue participant credentials |
| `viewer` | the loopback viewer handshake, or `EnvironmentHarness.viewer_credential()` | Read-only inspection. It cannot mutate anything, ingest evidence, create a dataset, or issue a credential |
| `participant` | only `POST /v1/sessions/{session_id}/participants/{participant_id}/credentials` (or `EnvironmentSession.participant_credential()`) | Observation reads and action submission for **one** session, participant, and generation, plus its own artifacts |

A fourth policy, `trusted-local`, exists only for the in-process Python facade.
It is never issuable as a credential; `EvidenceStore._issue` rejects it.

Training integrations are never executed over HTTP, so no issuable policy
carries `training.execute`. The participant surface is likewise unreachable
from a management credential: acting as a participant always requires a
participant-scoped context.

## The `401` versus `403` boundary

- **`401 unauthorized`** — the credential is missing, malformed, expired,
  revoked, or otherwise invalid. Authentication failed, so no policy was
  consulted.
- **`403 forbidden`** — the credential is valid but its server-owned policy, or
  its session/participant/generation constraint, denies the operation.

Authorization-sensitive lookups deliberately collapse *missing* and
*inaccessible* into the same `403` with the same message, so an out-of-scope
credential cannot enumerate resources. Neither response leaks credential
material.

## The unauthenticated in-process SDK

The direct Python SDK is a trusted local interface and requires no
authentication. `EnvironmentHarness` is its sole public execution facade:

```python
from environment_harness import EnvironmentHarness, Scenario

harness = EnvironmentHarness(
    ".environment-harness",
    environment_factory=MyEnvironment,
    agent_factories={"alice": MyAgent},
)
session = harness.run(Scenario(id="demo", input={}), turns=5)
print(session.status, session.verify())
```

`harness.run(...)` returns an `EnvironmentSession` handle. The handle is a typed
domain object: it does not inherit from, expose, or double as the
authorization-aware runtime, and every public method accepts only domain
inputs. No public callable accepts or returns a principal, an access context, a
role, a kind, or a permission list.

Behind the facade, a private `_SessionRuntime` requires a private
`_AccessContext` on every operation that can observe or mutate session state:

- `EnvironmentHarness` creates a trusted local context;
- HTTP authentication resolves bearer credentials into policy-constrained
  contexts; and
- the session runner derives participant-scoped contexts through
  `participant_context`, which is the same shape a remote participant
  credential resolves to.

Because all three paths drive one runtime, local, HTTP, and runner
authorization cannot drift into separate implementations.

The one public extension point that runs inside a session — a custom
`SessionRunner` — receives a typed `SessionControl` rather than the runtime and
the access context. The control binds both internally, so extension code cannot
forge, widen, or forward an authorization value, and `SessionControl` exposes
only domain operations. `EvidenceStore` is the exception by design: it is the
private storage seam that `EnvironmentHarness` drives, not a domain facade, and
its evidence readers still require an access context. A contract test enumerates
the domain facades and fails if any of them grows an authorization parameter.

## Issuing and constraining participant credentials

```python
session = harness.run(scenario, turns=5)
token = session.participant_credential("alice", ttl=1800)
```

or over HTTP with a management credential:

```http
POST /v1/sessions/{session_id}/participants/alice/credentials
Authorization: Bearer <management-credential>

{"ttl": 1800}
```

The issued credential is bound to that session, that participant, and the
participant's current generation. It cannot read or act outside those bounds:
transferring participant authority increments the generation and invalidates
the previous credential. Issuing a participant credential is a separate,
purpose-specific operation, so no other route — including authority transfer —
can mint remote access.

## The worker-protocol secret

The separate worker application created by `create_worker_app` keeps its own
private shared secret, its own `environment-worker.v1` protocol, its own size
boundary, and no OpenAPI document. It has no evidence-store access and does not
become a principal on the public session API. `/v1/worker/call` is not part of
the main HTTP migration inventory.

## Credential-store migration and forced reissue

Removing `Principal.role` is a **stored-shape** change, not only a wire change.
The `credentials` table persisted the whole `Principal` as JSON and revalidated
it on read against a strict `extra="forbid"` model, so every pre-change row
fails to parse once the field is gone.

The project treats all pre-`0.3.0rc1` stored credentials as disposable and
accepts a breaking forced reissue rather than carrying the discarded role
taxonomy into a compatibility mapper. Migration `005_credential_policies`:

- deletes every legacy credential row inside one transaction; and
- recreates `credentials` with `tenant`, `subject`, `policy`, `session`,
  `participant`, `generation`, `expires`, and `revoked` columns.

It runs automatically when a SQLite store is opened (recorded in
`schema_migrations`) and as a numbered SQL migration for PostgreSQL. It is
idempotent, and it changes no evidence, artifact, or session row.

**After upgrade, every existing bearer token returns `401`.** Callers must
reissue through `environment-harness token`, the embedding API, or the
participant-credential operation.

This is the general rule for this project: any change that alters a persisted
row shape needs its own numbered migration and its own downstream-impact entry.
Do not justify a credential break by claiming tokens are necessarily
short-lived — the previous API allowed long TTLs.

## The embedding-product delegation seam

An embedding product may authenticate its own users however it wishes and then
call the trusted administrative API in-process:

```python
token = harness.management_credential(subject="acme/user-42", ttl=900)
```

The product owns its user model, organization scoping, and audit trail.
EnvironmentHarness owns only the credential's policy and constraints.

Adding a new remote delegation policy later requires a concrete use case, a
threat-model review, explicit resource and action constraints, and fail-closed
tests. It does not require changing the public domain models.

## The access registry fingerprint

The registered action vocabulary, the three issuable policies, and their resource-constraint rules
are digested into one fingerprint. The HTTP migration gate recomputes it, so an authorization change
cannot hide inside a route rename or a response migration:

```sh
uv run python -c "from environment_harness.access import registry_fingerprint; print(registry_fingerprint())"
```

Any deliberate change to whether authentication is required, which credential policies may call an
operation, or how session and participant constraints are evaluated is classified as an
authorization change. It requires a security rationale and policy-by-policy contract tests. The
generated tables live in [HTTP migration](HTTP-MIGRATION.md).

## Digest threat model

Canonical digests provide reproducible identity, change detection, and
evidence-chain integrity when the verifier trusts its copy or source boundary.
They do **not** authenticate data against an attacker who controls the store. An
unsigned digest is not proof of provenance or tamper resistance against the
store operator; authenticity requires the separately specified signature or
attestation boundary in `docs/PROTOCOL.md`.
