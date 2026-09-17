# EnvironmentHarness

EnvironmentHarness runs agents in persistent shared environments. Inspect what each agent observed, save a checkpoint, branch the environment, and compare the recorded outcomes. Local examples need no account or model API key.

The MIT-licensed SDK includes local execution, an authenticated HTTP service, Python and TypeScript clients, and an evidence viewer. The included environment and agents are synthetic protocol examples. Their scores do not measure model intelligence or safety.

[Quickstart](#install-and-run) · [Connect an agent](docs/AGENT-INTEGRATION.md) · [Implement an environment](docs/AUTHORING.md) · [Protocol](docs/PROTOCOL.md) · [Release scope](docs/STATUS.md)

## Install and run

On macOS or Linux, install Python 3.12 or later and [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
git clone --branch v0.1.0 --depth 1 https://github.com/pollice-verso/environment-harness.git
cd environment-harness
uv sync --extra server
uv run python examples/branch_comparison.py --store .local/branch-demo
uv run environment-harness --store .local/branch-demo serve --open
```

The viewer opens and connects automatically at `http://127.0.0.1:8765`. No account or credential copying is needed. The browser exchanges a single-use connection link, valid for five minutes, for the local tenant credential and removes the link from the address bar. The local viewer keeps that credential in tab-scoped session storage so refreshing the tab stays connected. API requests still require authentication.

Omit `--open` when running without a browser. You can then open the viewer and paste the credential from the printed `researcher-token` file. Manually entered credentials stay in page memory. On macOS, `pbcopy < .local/branch-demo/researcher-token` copies the credential without printing it. Do not share credentials or connection links.

The example runs Alice and Bob for three turns, checkpoints their shared total of 6, and creates a branch with its total set to 20. It runs both environments for two more turns: the parent ends at 10, the branch at 24. Each participant receives its own synthetic private field. The two environments share a lineage; they are not independent samples.

Select either environment in the viewer. Filter the timeline by participant and observation, expand the recorded payload, and inspect the checkpoint. Select both environment checkboxes and click **Compare selected environments**. **Export evidence** downloads the selected environment's complete JSONL history. The script also writes `demo.json` and both JSONL exports into the store directory.

The wheel contains the built viewer. Node.js is only needed when changing its TypeScript source. Core installation without the optional server dependencies supports the Python API, CLI and evidence exports:

```sh
python -m pip install .
environment-harness doctor
```

## Work with an environment

Replace `ENVIRONMENT` and `CHECKPOINT` with the printed identifiers. Use the same store for all commands.

```sh
uv run environment-harness --store .local/branch-demo attach ENVIRONMENT
uv run environment-harness --store .local/branch-demo checkpoint ENVIRONMENT
uv run environment-harness --store .local/branch-demo branch ENVIRONMENT CHECKPOINT --interventions '{"total": 20}'
uv run environment-harness --store .local/branch-demo resume ENVIRONMENT
uv run environment-harness --store .local/branch-demo replay ENVIRONMENT
uv run environment-harness --store .local/branch-demo compare ENVIRONMENT OTHER_ENVIRONMENT
uv run environment-harness --store .local/branch-demo export ENVIRONMENT > trajectory.jsonl
uv run environment-harness --store .local/branch-demo cancel ENVIRONMENT
```

Replay reads evidence without loading an environment or invoking an agent. Resume marks the latest committed state ready to continue; the CLI command and viewer button do not execute additional agent turns. Call `run(...)` with the registered agents to advance it, as shown in [the complete example](examples/branch_comparison.py). Resume never rewinds successful external effects. Branch restores an immutable checkpoint into a separate environment. It rejects unsupported counterfactuals and checkpoints with pending decisions or external operations.

The viewer displays event timelines, participant perspectives, model/tool activity, artifact links, checkpoints, branch controls, scores, costs, frozen experiments and environment comparisons. It retains the latest 500 events; full history is available through paginated API reads and exports. Researcher perspective selection is a display filter. Participant API credentials enforce the actual access boundary.

## Python API

```python
from environment_harness import AgentSpec, EvidenceStore, ExperimentSpec, Principal, EnvironmentSession
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.runner import run

store = EvidenceStore(".local/experiment")
definition = SyntheticEnvironment(mode="simultaneous")
researcher = Principal(tenant="local", subject="researcher", role="researcher")
experiment = ExperimentSpec(
    environment=definition.spec,
    participants=(AgentSpec(id="alice", implementation="synthetic-agent@1", policy_version="1"),),
)
session = EnvironmentSession(store, definition)
environment = session.create(experiment, researcher)
run(session, environment["id"], researcher, {"alice": SyntheticAgent()}, turns=5)
```

The local Python API is a trusted embedding interface. Callers able to access its store can already read its database. Untrusted participants use the authenticated HTTP API, isolated credentials and a container boundary.

`AgentProgram` implementations return JSON actions. `CommandAgent` accepts an explicit executable and sends one observation through stdin. Its output, environment, timeout and temporary workspace are bounded, but a local subprocess is not an OS security sandbox. Use `DockerBackend` or `ModalBackend` to isolate hostile programs. Both block networking by default. Networked tool/model access requires a separately scoped gateway.

`ProcessEnvironment` runs supplier code through a persistent JSON worker in a separate process. `python -m environment_harness.worker PLUGIN` is the supplier entrypoint. Durable state must be serializable through the environment contract. Arbitrary process memory is not a checkpoint.

## Supplier contract

Implement `EnvironmentSpec`, `initialize`, `observe`, `resolve` and `intervene`. Register the factory in the `environment_harness.environments` Python entry-point group. The same `EnvironmentSession` can run locally or behind `create_app(session)` on the supplier server. The supplier owns state and commits; clients receive only permitted projections and opaque checkpoint IDs.

Contracts are in [`contracts/`](contracts). [Protocol semantics](docs/PROTOCOL.md) describes scheduling, authentication, persistence and API operations. [Adapters](docs/ADAPTERS.md) records their exact capabilities. `environment_harness.conformance.check` executes one supplied transition and the advertised checkpoint path. It is a compatibility check, not live qualification.

`EventMeasurements` produces raw counts without treating inactivity as compliance. `CommandScorer` runs a scorer in a separate JSON process. Model and human reports preserve their own kind and provenance. Scorers submit immutable `ScoreReport` revisions through the store or HTTP API. They must use a scorer version frozen in the experiment. Findings reference delivered observations, actions, execution and consequence events. Domain-specific rules and graders stay with the supplier. `ReceiptSigner` provides optional Ed25519 supplier signatures; private signing keys stay outside the package.

## Training

```sh
uv run environment-harness --store .local/training quickstart --training --turns 10
uv run environment-harness --store .local/training export ENVIRONMENT --training > rollout.jsonl
```

Training requires both environment entitlement and a training experiment/split. Scenario and time-boundary assignments cannot cross training and held-out splits within a tenant. Branches inherit their lineage and split. Exports preserve participant identity, policy version, observations, actions, rewards, report revisions and termination/truncation. This exporter does not claim action-correlated token IDs or log probabilities; requests requiring them fail explicitly. External training systems perform optimization.

## Develop

```sh
uv sync --extra server
npm ci --prefix packages/typescript
uv run python scripts/build_viewer.py
uv run python scripts/build_contracts.py
uv run pytest -q
uv run ruff check src tests scripts examples
npm run typecheck --prefix packages/typescript
uv run python scripts/build_contracts.py --check
uv build
uv run python scripts/check_distribution.py
```

Checks use synthetic agents and no model or paid infrastructure. They establish local behavior only. Hosted capacity, prolonged operation, arbitrary process restoration and environment validity require separate evidence.

Run the public adapter example with its optional dependencies:

```sh
uv sync --extra server --extra pettingzoo
uv run --no-sync python examples/pettingzoo_rps.py
```
