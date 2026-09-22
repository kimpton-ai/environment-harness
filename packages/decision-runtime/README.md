# EnvironmentHarness Decisions

An independently versioned, optional companion for bounded environment decisions.
It uses the provider-neutral `EnvironmentOperation` interface. EnvironmentHarness
core neither imports this package nor depends on TypeSafe.

Install this directory with `pip install ./packages/decision-runtime`. Add
`[typesafe]` to install the optional HTTP transport. No package publication is
part of this implementation.

## Execution contract

`DecisionOperation` receives an immutable `BoundedInvocation`: an objective,
authority bindings, explicit step/time/cost limits, and an absolute UTC expiry.
An `EnvironmentControl` observes the environment, generates bounded questions,
compiles complete answers into a local command, admits it, executes one effect,
and verifies fresh evidence. The control may be the environment itself.

Questions support candidate IDs, booleans and bounded scores. Multiple answers
form one command. Compilation and native admission must validate their combined
meaning. Private verifier state and executable payloads belong in the local
control, outside `Observation.model_input` and question labels.

The native driver must call `before_dispatch()` after queueing and immediately
before its effect. This rechecks SDK authority and native admission. Drivers must
honor cancellation and deadlines, release controls on `stop()`, and provide
idempotent lookup by the original execution ID. A returned acceptance or start
acknowledgement is not proof that an effect completed.

A private SQLite child ledger records reservations, provider attempts, answers,
compiled commands, dispatch intent, native receipts and verification. Artifacts
are content hashed and linked by stable IDs. Observation, selection, admission,
execution and verification timings are recorded. Session and directive revisions
are separate. Controls also bind environment identity, recovery generation,
ownership, observation revision and stop epoch.

There are no automatic retries after submission or uncertain native effects.
One provider retry is allowed only after a proven uncharged pre-submission
failure. Unknown charges retain their reservations. `lookup()` returns a final
receipt only when charges and effects are resolved. The receipt accounting ID is
the parent operation ID, so application and SDK evidence can refer to the same
charge without spending it twice.

`RecordedSelector` consumes an already paid selection with an explicit application
accounting link. It does not infer again. Planner and selector evidence remain
separate. Per-question probabilities are not a joint action probability.

## Jev

`environment_harness_decisions.jev.JevDecisionSelector` supports TypeSafe Choice,
Noul and Score questions at the pinned `jev-1.13.0` model. Credentials are supplied
at runtime. A selected Jev profile requires Jev; there is no fallback. Other
profiles can provide their own selector.

Hard budgets require a verified conservative charge bound scoped to the encoded
request. The caller supplies that bound and its evidence source. Without it,
inference is rejected before submission. A token estimate is not a provider
spending limit. No currently bundled byte heuristic supplies this verification.
The transport disables retries and redirects. No paid request is used in tests.

## Migration and compatibility

`MigrationTransaction` supplies consistent SQLite backups, read-only reports,
source and destination identities, hashes, resumable phases and a final commit
marker. Consumers must hold their original controller lock, prove the controller
is stopped, and reconcile dispatches, charges and prepared successors before
commit. They create an actual paused SDK segment from preserved state and link
its accounting to the original application. Applying a migration never rewrites
original manifests, journals or saves and cannot increase an allowance.

`legacy_contracts` reads version-one motor records. `LegacyMotorOperation` is a
compatibility facade for single-step legacy game adapters. Ordinary execution
uses `DecisionOperation`; its successor ledger retains explicit native prepared
admission and original IDs. Unknown effects cannot be acknowledged away. New
composed-control integrations should implement `EnvironmentControl` directly.

Prepared successors are optional and require explicit native support. Sequential
execution remains the default. Civ does not enable prepared successors.

## Validation scope

Browser and stepped drone fixtures exercise candidate and composed controls.
Game adapter tests use recorded/synthetic fixtures. These establish interface
behavior only, not full-game acceptance, current deployment health, or an
execution-performance improvement. Execution defaults remain unchanged.

Run focused tests from the repository root:

```sh
PYTHONPATH=src:packages/decision-runtime/src python -m pytest packages/decision-runtime/tests
python -m build packages/decision-runtime
```
