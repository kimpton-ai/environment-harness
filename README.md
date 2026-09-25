# EnvironmentHarness

[![CI](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/kimpton-ai/environment-harness/graph/badge.svg?branch=main)](https://app.codecov.io/gh/kimpton-ai/environment-harness)
[![PyPI](https://img.shields.io/pypi/v/environment-harness.svg)](https://pypi.org/project/environment-harness/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Run agents in persistent shared environments, then inspect exactly what they observed, attempted, and changed.

EnvironmentHarness records participant-specific observations, actions, outcomes, checkpoints, score reports, and branch lineage as durable evidence. You provide the environment rules, agent programs, and grading method.

[Quickstart](#quickstart) · [Trajectories](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAJECTORIES.md) · [Training](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAINING.md) · [Connect an agent](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) · [Implement an environment](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) · [API reference](https://github.com/kimpton-ai/environment-harness/blob/main/docs/API-REFERENCE.md)

## What you can do

- Run one or more agents against stateful environment rules.
- Give each participant a different private observation of the same shared state.
- Record observations, attempted actions, executed outcomes, scores, costs, and artifacts.
- Checkpoint an environment session, branch it with a declared intervention, and continue both sessions.
- Inspect timelines, compare related environment sessions, and export evidence as JSONL.
- Validate typed scenarios and run related scenario/trial experiments concurrently.
- Project native or imported traces into one portable trajectory contract, then freeze reproducible
  snapshots and training-entitled datasets.

## Quickstart

Install Python 3.12 or later and the SDK with its local viewer:

```sh
python -m pip install "environment-harness[server]"
```

To install an editable source checkout instead, follow the [contributing guide](https://github.com/kimpton-ai/environment-harness/blob/main/CONTRIBUTING.md).

Create a synthetic review dataset and open its evidence viewer:

```sh
environment-harness --store ./environment-sessions quickstart --turns 3
environment-harness --store ./environment-sessions serve --open
```

The demo creates two experiments that expand five scenarios into 10 grouped environment sessions,
plus three standalone sessions. It records different participant rosters, turn counts, score reports,
a blocked invalid action, a finding, a checkpoint, and an artifact so every main viewer page has
useful data. It requires no account, model API key, or paid service. None of its synthetic values
measure model quality or safety.

`quickstart` writes durable evidence to `./environment-sessions`. `serve` opens that same store through an authenticated API and read-only viewer at `http://127.0.0.1:8765`. The loopback viewer receives local researcher access automatically; `--open` only launches the browser. Refreshing the page or restarting the server does not remove recorded progress. API clients still use an explicit credential from `environment-harness token`. The server binds only to your computer and does not deploy or publish the environment sessions. Press `Ctrl+C` to stop it.

The running service publishes its generated OpenAPI document at `/openapi.json` and interactive reference at `/docs`. The repository checks the committed [`contracts/openapi.json`](contracts/openapi.json) and versioned JSON Schemas in [`contracts/`](contracts/) for drift. [`docs/API-REFERENCE.md`](docs/API-REFERENCE.md) documents endpoints and examples; [`docs/PROTOCOL.md`](docs/PROTOCOL.md) defines the authority, lifecycle, activity-stream, recovery, and evidence semantics that OpenAPI alone cannot express.

![EnvironmentHarness Home showing grouped experiments and their environment sessions](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-home.png)

Use the viewer from broad context to specific evidence:

1. **Home** groups related environment sessions under their experiment and keeps standalone sessions visible.
2. **Experiment** records the shared scenarios, trials, participants, environment, operations, policy, and scoring configuration.
3. **Session** separates frozen configuration, turn-by-turn evidence, progression, and versioned reports.

All screenshots use the repository's synthetic examples. Their counters, rewards, and findings demonstrate the data model; they are not model-quality or safety measurements.

## Run from Python

A Python agent implements `act(observation) -> dict`. `EnvironmentHarness` creates a fresh environment and agent for each environment session, runs them in a bounded thread pool, and records progress in the store you provide.

```python
from environment_harness import EnvironmentHarness, Scenario
from environment_harness.fixtures import SyntheticEnvironment, SyntheticScenarioInput


class IncrementAgent:
    implementation = "increment-agent@1"

    def act(self, observation):
        return {"value": 1}


harness = EnvironmentHarness(
    ".local/my-environment",
    environment_factory=SyntheticEnvironment,
    agent_factories={"alice": IncrementAgent},
)

environment_session = harness.run(
    Scenario(
        id="first-run",
        input=SyntheticScenarioInput(starting_total=0),
    ),
    turns=5,
)
print(environment_session.id, environment_session.status)
```

Open the recorded environment session with the same store:

```sh
environment-harness --store .local/my-environment serve --open
```

The `implementation` string is a versioned identifier recorded with the evidence so a resumed environment session cannot silently run different agent code. Your integration keeps ownership of prompts, model providers, tools, credentials, and spending limits. External JSON programs can use `CommandAgent`; see the [complete source example](https://github.com/kimpton-ai/environment-harness/blob/main/examples/custom_agent.py).

The local Python API and command subprocess are trusted interfaces, not operating-system security sandboxes. Run hostile programs in an isolated backend with explicitly scoped network access.

## Run an experiment

`EnvironmentHarness` accepts factories so every environment session receives a fresh environment and fresh agents. A `Scenario[T]` freezes its validated input, optional JSON reference, and metadata. Experiments expand one frozen configuration across every scenario and trial while enforcing shared concurrency limits.

```python
from environment_harness import EnvironmentHarness, Scenario
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment, SyntheticScenarioInput

harness = EnvironmentHarness(
    ".local/experiment",
    environment_factory=SyntheticEnvironment,
    agent_factories={"agent": SyntheticAgent},
    max_concurrency=4,
)

result = harness.experiment(
    "synthetic sweep",
    [
        Scenario(id="negative-start", input=SyntheticScenarioInput(starting_total=-3)),
        Scenario(id="positive-start", input=SyntheticScenarioInput(starting_total=3)),
    ],
    trials=2,
    seed=42,
    turns=5,
).run()

print(result.completed, result.total)
```

Run the complete example and open the same store to review its experiment and child environment sessions:

```sh
python examples/typed_experiment.py --store .local/typed-experiment --turns 3
environment-harness --store .local/typed-experiment serve --open
```

The example prints the experiment ID, every environment-session ID, scenario/trial labels, deterministic seeds, and the viewer path. See [examples/typed_experiment.py](examples/typed_experiment.py) for the copyable source.

Use `experiment.start()`, `wait()`, `stop()`, and explicit `resume()` for lifecycle control. Standalone environment sessions use `harness.start(...)` or `harness.run(...)`. The low-level coordinator, explicit `ExperimentSpec`, and branching workflow remain available from `environment_harness.advanced`.

## Extend an environment

An environment package owns four ordinary Python methods: `initialize`, `observe`, `resolve`, and
`intervene`, plus a stable `EnvironmentSpec`. Add an `EnvironmentOperation` when the environment
also needs imperative work such as controlling a game, reading a simulator, or calling an engine.
Runtime clients and credentials stay on the Python object; only the operation's name, version, and
JSON configuration are frozen into evidence.

The complete [custom environment experiment](examples/custom_environment_experiment.py) is a
runnable extension example. It defines an operation, interleaves it with normal turns through the
typed `SessionRunner` seam, runs two scenarios across two trials, and records comparable metrics plus
evidence-linked findings for every environment session.

```sh
python examples/custom_environment_experiment.py \
  --store .local/custom-environment-experiment \
  --turns 3
environment-harness \
  --store .local/custom-environment-experiment \
  serve --open
```

Open the experiment path printed by the example. The experiment page shows the configuration shared
by all four environment sessions, including the supplied operation and scoring version.

![EnvironmentHarness experiment page showing frozen execution, environment, evaluation, and participant configuration](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-experiment.png)

A session's Overview shows its frozen configuration, Turns describes operation receipts alongside
agent and environment evidence, and Progression plots recorded signals over the selected turn range.

![EnvironmentHarness Progression page showing a recorded signal across eight turns](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-progression.png)

Reports summarizes versioned scores, uncertainty, and evidence-linked findings.

![EnvironmentHarness session report showing versioned metrics and uncertainty](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-reports.png)

The same operation class works in a standalone environment session or a grouped experiment;
grouping is an orchestration choice, not a different environment type. Start with the
[environment authoring guide](docs/AUTHORING.md), then use the
[external simulator example](examples/external_environment_experiment.py) when your implementation
owns a long-lived process or engine connection.

## Inspect and export trajectories

Environment sessions keep their existing append-only evidence journal. A trajectory is a portable,
digest-bound view of that trace; it is not another writer. Pauses and resumes appear as causally
linked execution segments, while collection, execution, termination, and verified outcome remain
independent states.

```python
trajectory = environment_session.trajectory()
snapshot = environment_session.snapshot()
for row in harness.sources().export_snapshot(snapshot.metadata.id):
    print(row)
```

The in-process SDK is a trusted local interface and needs no credential. Remote callers send only
an opaque bearer credential; the server resolves it to one of its fixed management, viewer, or
participant policies. See [Authentication](docs/AUTHENTICATION.md).

The same interface accepts hash-chained historical records from a namespaced external source.
Identical retries are idempotent, conflicting identities fail, and source health exposes the
acknowledged position/hash, backlog, gaps, and capture failures. See
[Trajectories and historical evidence](docs/TRAJECTORIES.md) for the Python, CLI, and authenticated
HTTP workflow.

Training datasets are immutable ordered snapshot selections and require complete, terminal,
training-entitled evidence with resolved reward chains. Trainer instances are injected locally;
the browser server never executes them. See [Frozen datasets and local training integrations](docs/TRAINING.md).

## Documentation

| Goal | Guide |
| --- | --- |
| Look up a resource, command, protocol, or status dimension | [Data models](https://github.com/kimpton-ai/environment-harness/blob/main/docs/DATA-MODELS.md) |
| Import, inspect, snapshot, and export trajectories | [Trajectories](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAJECTORIES.md) |
| Freeze datasets and invoke local training integrations | [Training](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAINING.md) |
| Connect a Python agent, model integration, or JSON program | [Agent integration](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) |
| Implement environment rules and custom operation classes | [Environment authoring](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) · [Complete experiment](https://github.com/kimpton-ai/environment-harness/blob/main/examples/custom_environment_experiment.py) |
| Connect an external simulator or engine | [External simulator experiment](https://github.com/kimpton-ai/environment-harness/blob/main/examples/external_environment_experiment.py) |
| Understand checkpoints, branches, and coordinated sessions | [Coordinated sessions](https://github.com/kimpton-ai/environment-harness/blob/main/docs/coordinated-sessions.md) |
| Branch and compare environment sessions | [Branch comparison example](https://github.com/kimpton-ai/environment-harness/blob/main/examples/branch_comparison.py) |
| Run environments behind a trusted supervisor | [Remote workers and external agents](https://github.com/kimpton-ai/environment-harness/blob/main/docs/REMOTE-WORKERS.md) |
| Authenticate the HTTP API and constrain credentials | [Authentication](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHENTICATION.md) |
| Use the authenticated HTTP API | [API reference](https://github.com/kimpton-ai/environment-harness/blob/main/docs/API-REFERENCE.md) · [Protocol semantics](https://github.com/kimpton-ai/environment-harness/blob/main/docs/PROTOCOL.md) |
| Use the TypeScript client | [TypeScript package](https://github.com/kimpton-ai/environment-harness/blob/main/packages/typescript/README.md) |
| Check adapter and isolation boundaries | [Adapters](https://github.com/kimpton-ai/environment-harness/blob/main/docs/ADAPTERS.md) |
| Check compatibility and release limits | [Compatibility](https://github.com/kimpton-ai/environment-harness/blob/main/docs/COMPATIBILITY.md) · [Release scope](https://github.com/kimpton-ai/environment-harness/blob/main/docs/STATUS.md) |
| Install, run, and evaluate deployment options | [Deployment](https://github.com/kimpton-ai/environment-harness/blob/main/docs/DEPLOYMENT.md) |
| Maintain the packaged browser UI | [Viewer maintenance](https://github.com/kimpton-ai/environment-harness/blob/main/docs/VIEWER-MAINTENANCE.md) |
| Prepare and publish a release | [Release process](https://github.com/kimpton-ai/environment-harness/blob/main/docs/RELEASING.md) |

## Project

[Contributing](https://github.com/kimpton-ai/environment-harness/blob/main/CONTRIBUTING.md) · [Support](https://github.com/kimpton-ai/environment-harness/blob/main/SUPPORT.md) · [Security](https://github.com/kimpton-ai/environment-harness/blob/main/SECURITY.md) · [Changelog](https://github.com/kimpton-ai/environment-harness/blob/main/CHANGELOG.md) · [MIT license](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Report vulnerabilities privately through [SECURITY.md](https://github.com/kimpton-ai/environment-harness/blob/main/SECURITY.md). Do not put credentials or private environment sessions in a public issue.
