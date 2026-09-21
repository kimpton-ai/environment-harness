# EnvironmentHarness

EnvironmentHarness is an open-source SDK for evaluating agents in persistent shared environments. It coordinates agent decisions, records observations and outcomes, and supports checkpoints and branching so you can inspect how behavior changes across conditions.

You provide the environment, agents and grading rules. The harness manages the session lifecycle and records the evidence those rules use. The MIT-licensed SDK includes local execution, an authenticated HTTP service, Python and TypeScript clients, and an evidence viewer. Local installation checks need no account or model API key.

[Evaluation workflow](#how-an-evaluation-works) · [Install](#install-and-run) · [Connect an agent](#connect-your-agent) · [Define an environment](#define-your-environment) · [Grading](#grade-and-interpret-results) · [Release scope](docs/STATUS.md)

## When to use it

Use EnvironmentHarness when an evaluation depends on state that persists across decisions, information that differs between participants, or interactions among agents. An environment can contain one or several agents and resolve their actions sequentially or in simultaneous phases.

The environment defines what participants can observe, which actions they can take and how those actions change shared state. This lets you evaluate behavior over a sequence of consequences. Domain rules determine whether an action completes a task, violates a constraint or creates an outcome that can only be graded later.

Checkpoints preserve supported environment and agent state. Branches let you apply a declared intervention and continue from that checkpoint in a separate session. Each branch retains its relationship to the original session, so comparisons can identify which conditions changed and which history was shared.

## How an evaluation works

1. **Define the environment.** Specify initial state, permitted observations, action schemas, transition rules and supported interventions. Keep state serializable and declare the scheduling and recovery capabilities your implementation supports.
2. **Register the agents.** Connect your own model integration, agent framework or deterministic program. Record each participant's implementation, policy version and configuration in the experiment.
3. **Freeze the experiment.** Select the environment version, participants, seed, execution policy and scorer versions. These records identify the system being evaluated.
4. **Run the session.** The runner delivers participant-specific observations and collects actions. The environment resolves those actions into new state and outcomes. The harness records delivered observations, decisions and committed events across turns.
5. **Grade the evidence.** Your scorer evaluates recorded behavior and consequences, then submits a versioned report tied to the evidence it inspected. Metrics and their interpretation belong to your evaluation design.
6. **Inspect and compare.** Read participant timelines, inspect findings, export the evidence or compare sessions. For supported environments, checkpoint and branch through the CLI or SDK to study an intervention.

For repeated evaluations, create separate sessions with recorded seeds and configurations. Hold relevant conditions fixed when comparing agents and report the variation across runs. Branches share history and must not be counted as independent trials. The viewer displays recorded comparisons; your analysis determines whether they support a broader conclusion.

## What you provide and what the harness handles

| Component | You provide | EnvironmentHarness handles |
| --- | --- | --- |
| Environment | State, rules, observations, action schema and supported interventions. | Session coordination, action delivery, committed events and checkpoint storage. |
| Agent | Model or program, prompts, tools, memory, provider transport and spending controls. | Versioned participant registration, observation delivery and durable dispatch records. |
| Grader | Metrics, success criteria, constraint checks and evidence that the grading method is valid. | Versioned score reports, evidence references and retained report revisions. |
| Experiment | Participants, conditions, seeds, execution limits and comparison design. | Frozen experiment records, session identities and branch lineage. |
| Deployment | Dependencies, credentials and appropriate isolation for the code you run. | Local runtime, authenticated service and optional execution/storage adapters. |

You receive recorded observations, actions and outcomes, plus any instrumented model/tool activity, artifacts and score reports your integration produces. The read-only viewer exposes that evidence by revision and participant. JSONL exports support analysis outside the viewer. Model and tool internals are recorded only when your integration instruments them.

## Install and run

The included counter and agents are synthetic installation checks. They demonstrate persistence, private observations and branching; their scores do not measure model intelligence or safety.

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

Select either environment in the viewer. The timeline groups evidence by state revision, with one row per participant showing the observation, the attempted action and the executed outcome. Filter by participant perspective or activity, expand any cell for its recorded payload, and open a checkpoint row for the command that branches from it. Select both environment checkboxes and click **Compare selected environments**. **Export evidence** downloads the selected environment's complete JSONL history. The script also writes `demo.json` and both JSONL exports into the store directory.

The wheel contains the built viewer. Node.js is only needed when changing its TypeScript source. Core installation without the optional server dependencies supports the Python API, CLI and evidence exports:

```sh
python -m pip install .
environment-harness doctor
```

## Work with an environment

Replace `ENVIRONMENT` and `CHECKPOINT` with the printed identifiers. Use the same store for all commands.

```sh
uv run environment-harness --store .local/branch-demo list
uv run environment-harness --store .local/branch-demo show ENVIRONMENT
uv run environment-harness --store .local/branch-demo timeline ENVIRONMENT --participant alice
uv run environment-harness --store .local/branch-demo attach ENVIRONMENT
uv run environment-harness --store .local/branch-demo checkpoint ENVIRONMENT
uv run environment-harness --store .local/branch-demo branch ENVIRONMENT CHECKPOINT --interventions '{"total": 20}'
uv run environment-harness --store .local/branch-demo resume ENVIRONMENT
uv run environment-harness --store .local/branch-demo replay ENVIRONMENT
uv run environment-harness --store .local/branch-demo compare ENVIRONMENT OTHER_ENVIRONMENT
uv run environment-harness --store .local/branch-demo export ENVIRONMENT > trajectory.jsonl
uv run environment-harness --store .local/branch-demo cancel ENVIRONMENT
```

`list`, `show` and `timeline` print readable summaries of recorded environments. `timeline` groups events by the state revision an action was taken from and prints one line per participant with the observation, the attempted action and the executed outcome. `--participant` limits it to the evidence that participant could see, `--kind` filters event kinds, and `--verbose` includes the recorded payloads. `compare` prints a summary; `--json` on any of these commands prints the underlying records. `attach`, `replay` and `export` remain JSON for scripts.

Replay reads evidence without loading an environment or invoking an agent. Resume marks the latest committed state ready to continue; the CLI command does not execute additional agent turns. Call `run(...)` with the registered agents to advance it, as shown in [the complete example](examples/branch_comparison.py). Resume never rewinds successful external effects. Branch restores an immutable checkpoint into a separate environment. It rejects unsupported counterfactuals and checkpoints with pending decisions or external operations.

The viewer reads evidence and never writes to an environment. It shows the timeline grouped by revision with per-participant observation, action and outcome, participant perspectives, model and tool activity, artifact links, checkpoints, scores, costs, the frozen experiment and environment comparisons. Checkpoint, resume, cancel and branch are command-line and SDK operations. It retains the latest 500 events; full history is available through paginated API reads and exports. Researcher perspective selection is a display filter. Participant API credentials enforce the actual access boundary.

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

## Connect your agent

A Python agent implements `act(observation) -> dict` and returns an action matching the environment's schema. `CommandAgent` supports an external program that reads an observation as JSON from stdin and writes a JSON action to stdout. Your existing agent can keep its own prompts, model provider and tool logic behind this interface.

Your integration supplies provider credentials, request handling and spending limits. `InstrumentedModel` can record model requests and responses around your generation callback. Credentials are not automatically inherited by command agents. To restore agent memory across checkpoints, implement the explicit `checkpoint()` and `restore(state)` hooks and declare that capability when registering the agent.

See [agent integration](docs/AGENT-INTEGRATION.md) for the program contract and [adapter capabilities](docs/ADAPTERS.md) for framework-specific boundaries. An available adapter does not imply that its upstream dependency or cloud service has been qualified.

## Define your environment

Implement the following contract:

| Member | Responsibility |
| --- | --- |
| `spec: EnvironmentSpec` | Declare the environment identity, version, action schema, scheduling and capabilities. |
| `initialize(experiment)` | Construct the initial serializable state. |
| `observe(state, participant)` | Return the information that this participant is permitted to see. |
| `resolve(state, actions, random, events)` | Apply the rules and return updated state, participant outcomes and events. |
| `intervene(state, changes)` | Apply supported branch interventions and reject unsupported changes. |

Register the factory in the `environment_harness.environments` Python entry-point group. The same `EnvironmentSession` can run locally or behind `create_app(session)` on the supplier server. The environment implementation owns state and transition rules; remote clients receive only permitted projections and opaque checkpoint IDs. The [authoring guide](docs/AUTHORING.md) covers packaging and checks.

Contracts are in [`contracts/`](contracts). [Protocol semantics](docs/PROTOCOL.md) describes scheduling, authentication, persistence and API operations. [Adapters](docs/ADAPTERS.md) records their exact capabilities. `environment_harness.conformance.check` executes one supplied transition and the advertised checkpoint path. It is a compatibility check, not live qualification.

## Grade and interpret results

The environment produces outcomes and events. Your grader decides what they mean for the evaluation. Define success criteria, constraint violations, incomplete outcomes and the evidence each finding requires. Preserve execution failures separately from conclusions about agent behavior, and check the grader against known valid and invalid behavior before relying on its scores.

`EventMeasurements` produces raw counts without treating inactivity as compliance. `CommandScorer` runs a scorer in a separate JSON process. Model and human reports preserve their own kind and provenance. Scorers submit immutable `ScoreReport` revisions through the store or HTTP API using a scorer version frozen in the experiment. Findings reference delivered observations, actions, execution and consequence events. A later report is a retained revision rather than an overwrite of the earlier conclusion.

The harness preserves evidence and report identity. The quality of a benchmark still depends on its tasks, grader and experimental design. It does not provide a universal safety score or certify that a custom metric measures the intended capability. `ReceiptSigner` provides optional Ed25519 supplier signatures; private signing keys stay outside the package.

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
npm test --prefix packages/typescript
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

[Compatibility details](docs/COMPATIBILITY.md) cover cancellation, agent registration, score groups and inherited evidence.

Optional [motor control](docs/motor-control.md) adds bounded native, browser and desktop skills with durable receipts and an optional Jev selector. Direct execution remains the default.
