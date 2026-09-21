# Remote workers and external agents

`python -m environment_harness.worker_server PLUGIN` runs the serializable native
environment contract on port 8080. Supply a distinct `ENVIRONMENT_WORKER_TOKEN`
of at least 32 characters and put the service behind authenticated HTTPS. The
entrypoint removes this bootstrap variable before loading supplier code.

The trusted supervisor constructs `RemoteEnvironment(HTTPWorkerTransport(...),
expected_spec=admitted_spec)` and passes it to `EnvironmentSession`. Only that
supervisor has the evidence database and artifact credentials. Workers receive
explicit state, actions, events and RNG state. A lost worker can be replaced
only with the same admitted implementation. Native methods must be pure with
respect to serialized inputs. External effects require the existing durable
operations protocol. A container boundary alone cannot make impure code safe
to replay.

External participants obtain scoped credentials, read observations and submit
versioned actions. The supervisor calls `coordinator.advance` after submissions
and from a deadline scanner. It resolves at most one ready phase under the
normal renewable, fenced writer lease. An incomplete phase stays open unless
its declared deadline and missing-action policy permit advancement. This loop
does not execute models or update model weights.

`adapters.legacy.LegacyEnvironment` wraps a serializable `world-session.v1`
implementation under a distinct native version. Old manifests, evidence and
stores remain unchanged and need their original reader. This is a new execution
identity, not a migration of historical records.

`adapters.ors.ORSClient` speaks the ORS HTTP and SSE protocol without modifying
the supplier server. Persist its upstream session ID and each tool task ID
before exposing results. On stream interruption, reconnect only with the
recorded task ID. If dispatch occurred without a task receipt, the outcome is
unknown. Never repeat the tool with a new task ID to resolve uncertainty.
Expired upstream receipts and lost memory-only servers cannot establish whether
an action completed. Keepalive, container ownership, resource cleanup and
accounting belong to the host. ORS session handles are not native checkpoints.

Both HTTP transports disable ambient proxies and redirects, bound response
sizes and timeouts, redact upstream errors, and perform no implicit retries.
They assume the host supplies a trusted endpoint. Supplier-selected URLs require
host-side admission and network isolation. Test fixtures are protocol checks,
not evidence of hosted capacity or supplier measurement quality.


Trusted PostgreSQL hosts may set `max_retained_bytes` to cap aggregate environment
payload storage. State, evidence, checkpoints and artifact bytes count toward the
allowance. The host must qualify the storage-query cost for its workload.

`PostgresEvidenceStore.purge_tenant` is a privileged retention method, not a session
HTTP operation. The caller must stop admission and confirm that every tenant
worker has stopped. It refuses active writer leases, erases exact environment
artifact prefixes, and deletes tenant records only after object deletion succeeds.
Ordinary evidence remains immutable. The default S3 retention adapter refuses
versioned buckets because deleting current keys would not erase older versions.
