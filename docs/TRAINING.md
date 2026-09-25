# Frozen datasets and local training integrations

EnvironmentHarness records and exports training inputs; it is not a managed trainer. Training code
runs only when trusted Python or the CLI explicitly injects a `TrainingIntegration`. The HTTP
server can display recorded results but has no route that executes an installed trainer.

## Eligibility

A trajectory can enter a dataset only when all of these are true:

- its frozen purpose and split are both `training`;
- collection is complete and execution is completed;
- it is terminated or truncated with a resolved outcome;
- the researcher can read the full selected evidence;
- every reward supersession chain is complete, acyclic, unambiguous, and finite; and
- every member uses the same source schema version.

Evaluation or heldout trajectories can still be inspected and snapshotted. They cannot be relabeled
as training data.

## Inference capture levels

Only in-process Python agents that call `InstrumentedModel` can provide token-faithful inference
evidence. `CommandAgent` and remote HTTP participants remain valid evaluation transports, but their
trajectories cannot be represented as token-faithful training input. This is a product capability
boundary, not a missing optional field on `Action`: `Action` has no inference fields, and a
transport-neutral inference endpoint needs its own security and protocol design.

The session's frozen `RunPolicy.inference_capture` decides how much is recorded. A caller cannot
widen capture beyond its experiment's entitlement.

| Level | Records | Requires |
| --- | --- | --- |
| `none` | Nothing. Provider validation still runs, so a malformed response fails closed. | — |
| `summary` (default) | Model, tokenizer, and renderer identity, seed, usage, timing, token counts, finish reason, and validation state — no rendered content, token IDs, or log probabilities. | — |
| `training` | Everything `summary` records plus rendered requests and responses, token IDs, and log probabilities, spilled to audience-restricted artifacts when an event would exceed `max_event_bytes`. | Training entitlement (`purpose="training"` and `split="training"`) and a bounded `max_inference_artifact_bytes` |

```python
from environment_harness.contracts import RunPolicy

policy = RunPolicy(
    inference_capture="training",
    max_inference_artifact_bytes=256 * 1024 * 1024,
)
```

A `training` policy with an unbounded cumulative budget is rejected **before execution**, not
partway through a run. Oversized detail fails explicitly rather than being silently truncated:
`InstrumentedModel` charges each spilled artifact against the cumulative per-Session budget and
raises when the budget is exhausted. Credentials and provider session URLs never appear in these
records.

### Representative storage estimates

Local SQLite users choose retention and capture settings knowingly. These are order-of-magnitude
estimates from the synthetic fixtures; your own tokenizer, prompt length, and multimodal content
dominate the real numbers.

| Capture | Per model call | 1,000 calls | 100,000 calls |
| --- | --- | --- | --- |
| `none` | 0 | 0 | 0 |
| `summary` | ~0.5 KiB | ~0.5 MiB | ~50 MiB |
| `training`, 2,000 output tokens, no content | ~30 KiB | ~30 MiB | ~3 GiB |
| `training` with rendered request and response | ~60 KiB | ~60 MiB | ~6 GiB |

`summary` is the default because it keeps a full evaluation record at roughly 1% of the storage a
token-faithful capture needs. Set `max_inference_artifact_bytes` to the ceiling you are willing to
retain per Session; the default 64 MiB suits an experiment-sized run rather than a long campaign.

## Freeze and stream a dataset

```python
trajectories = harness.sources()

dataset = trajectories.freeze_dataset("synthetic-training", ("ENVIRONMENT_SESSION_ID",))

for row in trajectories.export_dataset(dataset.metadata.id):
    process(row)
```

`harness.sources()` is the trusted local management surface. It requires no credential because the
in-process SDK is a trusted interface; the authenticated HTTP API never executes a training
integration. See [Authentication](AUTHENTICATION.md).

Dataset identity is derived from the canonical selection manifest and ordered trajectory digests,
not local paths. Every member points to an immutable trajectory snapshot. Later evidence does not
change an existing dataset export.

The CLI equivalents are:

```sh
environment-harness --store .local/evidence dataset-create NAME SESSION_ID...
environment-harness --store .local/evidence dataset-show DATASET_ID
environment-harness --store .local/evidence dataset-export DATASET_ID > dataset.jsonl
```

Core export is streaming JSONL. Parquet is not in the `0.3.0rc1` release manifest; add it only with
a separately qualified optional dependency and canonical-equivalence tests.

## Integration protocol

An integration instance keeps live clients, credentials, framework objects, and provider state in
memory. Only its identity/version, the dataset digest, a digest of configuration, and its returned
receipt are recorded.

```python
from environment_harness.training import TrainingOutput


class LocalTrainer:
    identity = "com.example.local-trainer"
    version = "1"

    def validate(self, dataset):
        if dataset.status.reward_state != "ready":
            raise ValueError("this trainer requires resolved rewards")

    def train(self, dataset, config):
        # Inject and use live framework/model clients here. Do not return them.
        return TrainingOutput(
            policy=trained_policy_resource,
            metrics={"loss": 0.25},
            artifacts=(),
            limitations=("synthetic example",),
        )


recorded = training.run(dataset.metadata.id, LocalTrainer(), {"epochs": 1}, researcher)
```

Third-party packages can publish integration factories under the
`environment_harness.training` entry-point group. The CLI loads exactly one requested entry point:

```sh
environment-harness --store .local/evidence train DATASET_ID ENTRY_POINT_NAME \
  --config '{"epochs":1}'
```

## Inference evidence

`InstrumentedModel` is the initial token-faithful capture boundary. During a runner call it records
the durable agent-work ID, observation ID, participant, generation, and revision on request,
response, and failure evidence. Token IDs must be non-negative integers; log probabilities must be
finite and non-positive; paired arrays must have equal lengths. Content capture is opt-in.

When rendered content, tokens, or log probabilities would exceed the environment's event limit,
the detail is stored as a participant-scoped JSON artifact and the event keeps only its digest-bound
reference and summary. The artifact limit still applies and oversized evidence fails explicitly.
`CommandAgent` and remote HTTP agents do not attach token-faithful inference evidence.

## Optional framework status

The `verifiers` extra is bounded to `>=0.3.1,<0.4`. Its legacy rollout invocation remains optional,
and authorized rows now derive from the canonical trajectory resource rather than the removed
action-row authority. CI installs the bounded extra and checks that bridge independently.

RLlib episode conversion, RLlib external environments, TRL `environment_factory`, live OpenEnv
training, and a native Verifiers v1 `Episode` converter are intentionally not in the frozen
`0.3.0rc1` manifest. Those APIs own different execution loops and change independently. They must
land as optional integrations with real-version fixture tests; EnvironmentHarness must not claim
support by returning a similarly shaped dictionary. Ray, Torch, TRL, and Verifiers are never core
dependencies.

## Security and deployment boundary

The viewer is read-only. `/v1/training-runs` and `/v1/training-runs/{id}` read immutable local
receipts; no HTTP endpoint imports an asserted result or runs arbitrary trainer code. Managed jobs,
GPU scheduling, remote worker control, checkpoint promotion, billing, and model deployment remain
outside this SDK.
