# Authentication and authorization

EnvironmentHarness implements **no users, groups, organizations, OAuth, or RBAC**. There are two
access paths, and only one of them involves a credential:

| Path | Credential | Boundary |
| --- | --- | --- |
| In-process Python and the CLI | **none** | Filesystem permissions on the evidence store |
| HTTP | opaque bearer token | The server-owned policy persisted beside that token |

An embedding product authenticates its own users however it wishes and uses the in-process seam to
issue constrained EnvironmentHarness credentials.

## Local access is not authenticated

```python
harness = EnvironmentHarness(".environment-harness", environment_factory=MyEnvironment,
                             agent_factories={"alice": MyAgent})
```

No token, configuration file, keychain, or credential chain is consulted. Internally this path
carries a `trusted-local` access context, which is **not a credential**: `EvidenceStore._issue`
refuses to mint it, so it can never appear on the wire. Whoever can open the store file already has
full authority over it, exactly as with SQLite, a local MLflow tracking directory, or a Jupyter
kernel. Protect the store with file permissions; the SDK does not add a second boundary.

The in-process API is also a trusted embedding boundary, not a sandbox. Run untrusted participants
through scoped HTTP credentials and an isolated backend.

## The bearer-credential contract

A remote client sends exactly one thing:

```http
GET /v1/sessions HTTP/1.1
Authorization: Bearer <opaque-credential>
```

The credential is an opaque random string. The server stores its SHA-256 hash beside the policy and
resource constraints it was issued with, and never trusts a policy, permission, role, or scope
supplied in a request body, query parameter, or header. Adding `{"policy": "admin"}` to a
request cannot widen access: strict command models reject unknown fields, and the persisted policy
is the only input to the check. OpenAPI publishes only the standard HTTP bearer security scheme —
no `x-roles`, `x-principal-kinds`, or other custom caller taxonomy.

## The three issuable policies

These name credential behavior. They are not users, organization membership, or public roles.

| Policy | Issued by | May do |
| --- | --- | --- |
| `admin` | `environment-harness token`, or `EnvironmentHarness.admin_credential()` | Create experiments and sessions, run lifecycle commands, read full evidence, register and ingest sources, freeze snapshots and datasets, read recorded training results, and issue participant credentials |
| `viewer` | the loopback viewer handshake, or `EnvironmentHarness.viewer_credential()` | Read-only inspection. It cannot mutate anything, ingest evidence, create a dataset, or issue a credential |
| `participant` | only `POST /v1/sessions/{id}/participants/{participant}/credentials`, or `EnvironmentSession.participant_credential()` | Observation reads and action submission for **one** session, participant, and generation, plus its own artifacts |

Each exists for a specific reason:

- **`viewer` is read-only because it is handed out without proof of identity.** `POST /local/connect`
  returns it to any loopback caller presenting the expected origin, so that the packaged browser UI
  can connect with no user action. If that token could mutate, opening a browser tab would grant
  write access to anything able to reach loopback.
- **`participant` is an experimental-validity control, not only a security control.** The
  participant is the agent under evaluation. If it could read another participant's observations or
  evidence outside its turn, the experiment would measure the wrong thing. It is therefore scoped to
  a `(session, participant, generation)` triple rather than to a caller, and transferring
  participant authority increments the generation and invalidates the previous token mid-session.
- **`admin` is the ordinary "I own this store" credential** — the closest thing here to a
  conventional API key.

Training integrations never execute over HTTP, so no issuable policy carries `training.execute`.
The participant surface is likewise unreachable from a admin credential: acting as a
participant always requires a participant-scoped context.

## Issuing and constraining participant credentials

```python
token = session.participant_credential("alice", ttl=1800)
```

Issuing one is a separate, purpose-specific operation, so no other route — including authority
transfer — can mint remote access.

## The `401` versus `403` boundary

- **`401 unauthorized`** — the credential is missing, malformed, expired, revoked, or otherwise
  invalid. Authentication failed, so no policy was consulted.
- **`403 forbidden`** — the credential is valid but its policy, or its
  session/participant/generation constraint, denies the operation.

Authorization-sensitive lookups deliberately collapse *missing* and *inaccessible* into the same
`403` with the same message, so an out-of-scope credential cannot enumerate resources. Neither
response leaks credential material.

## One runtime, three ways in

A private `_SessionRuntime` requires a private `_AccessContext` on every operation that observes or
mutates session state. `EnvironmentHarness` supplies a trusted local context, HTTP authentication
resolves a bearer token into a policy-constrained one, and the session runner derives
participant-scoped contexts that are the same shape a remote participant credential produces.
Because all three drive one runtime, local, HTTP, and runner authorization cannot drift apart.

No public callable accepts or returns a principal, access context, role, kind, or permission list.
A custom `SessionRunner` receives a typed `SessionControl` that binds the runtime and context
internally, so extension code cannot forge, widen, or forward an authorization value.
`EvidenceStore` is the deliberate exception: it is the private storage seam, not a domain facade,
and its evidence readers still take an access context. A contract test enumerates the domain facades
and fails if any grows an authorization parameter.

## The worker-protocol secret

The separate worker application created by `create_worker_app` keeps its own private shared secret,
its own `environment-worker.v1` protocol, its own size boundary, and no OpenAPI document. It has no
evidence-store access and is not a caller on the public session API. `/v1/worker/call` is outside
the HTTP migration inventory.

## The access registry fingerprint

The registered action vocabulary, the three issuable policies, and their resource-constraint rules
digest into one fingerprint. The HTTP migration gate recomputes it, so an authorization change
cannot hide inside a route rename or a response migration:

```sh
uv run python -c "from environment_harness.access import registry_fingerprint; print(registry_fingerprint())"
```

Any deliberate change to whether authentication is required, which policies may call an operation,
or how session and participant constraints are evaluated is an authorization change. It requires a
security rationale and policy-by-policy contract tests. The generated tables live in
[HTTP migration](HTTP-MIGRATION.md).

## Digest threat model

Canonical digests provide reproducible identity, change detection, and evidence-chain integrity when
the verifier trusts its copy or source boundary. They do **not** authenticate data against an
attacker who controls the store. An unsigned digest is not proof of provenance or tamper resistance
against the store operator; authenticity requires the separately specified signature or attestation
boundary in [Protocol](PROTOCOL.md).

## Upgrading from the removed role model

`Principal` and its `researcher`/`agent`/`scorer`/`worker` values are gone, and every pre-`0.3.0rc1`
credential row is deleted by migration `005_credential_policies`. See
[Compatibility](COMPATIBILITY.md#what-upgrading-to-030rc1-does-and-does-not-change) for the
migration and [the release appendix](RELEASING.md#downstream-impact-appendix) for the required
consumer changes.
