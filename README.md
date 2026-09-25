# EnvironmentHarness

[![CI](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/kimpton-ai/environment-harness/graph/badge.svg?branch=main)](https://app.codecov.io/gh/kimpton-ai/environment-harness)
[![PyPI](https://img.shields.io/pypi/v/environment-harness.svg)](https://pypi.org/project/environment-harness/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Run agents in persistent shared environments, then inspect exactly what they observed, attempted, and changed.

EnvironmentHarness records participant-specific observations, actions, outcomes, checkpoints, score reports, and branch lineage as durable evidence. You provide the environment rules, agent programs, and grading method.

[Quickstart](#quickstart) · [Trajectories](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAJECTORIES.md) · [Training](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAINING.md) · [Decision runtime](https://github.com/kimpton-ai/environment-harness/blob/main/docs/DECISION-RUNTIME.md) · [Connect an agent](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) · [Implement an environment](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) · [API reference](https://github.com/kimpton-ai/environment-harness/blob/main/docs/API-REFERENCE.md)

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

No account, model API key, or paid service. The demo expands five scenarios into 10 grouped
environment sessions plus three standalone ones, with varied participant rosters, score reports, a
blocked action, a finding, a checkpoint, and an artifact, so every viewer page has real data. None
of its synthetic values measure model quality or safety.

`quickstart` writes durable evidence to `./environment-sessions`; `serve` opens that same store at
`http://127.0.0.1:8765`. The server binds to loopback only and publishes nothing. The browser
receives read-only access automatically — `--open` just launches it — while API clients need an
explicit credential from `environment-harness token`. Refreshing or restarting loses nothing.

The service publishes OpenAPI at `/openapi.json` and an interactive reference at `/docs`.
[API reference](docs/API-REFERENCE.md) covers endpoints and examples;
[Protocol](docs/PROTOCOL.md) covers the authority, lifecycle, recovery, and evidence semantics
OpenAPI cannot express.

![The EnvironmentHarness Sessions index showing grouped experiments and their environment sessions](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-home.png)

The viewer has four global destinations — **Overview**, **Experiments**, **Sessions**, and **Trajectories** — and moves from broad context to specific evidence:

1. **Sessions** groups related environment sessions under their experiment and keeps standalone sessions visible.
2. **Experiments** records the shared scenarios, trials, participants, environment, operations, policy, and scoring configuration, and lists the trajectories its sessions recorded.
3. A **session** separates its scores, turn-by-turn evidence, progression, trajectory, and frozen configuration.
4. **Trajectories** lists native and imported portable evidence with its collection health, records, snapshot boundaries, and provenance.

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
    environment=SyntheticEnvironment,
    agents={"alice": IncrementAgent},
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

Open it with `environment-harness --store .local/my-environment serve --open`.

`implementation` is a versioned identifier recorded with the evidence, so a resumed session cannot
silently run different agent code. Your integration owns prompts, model providers, tools,
credentials, and spending limits. External JSON programs use `CommandAgent` — see
[examples/custom_agent.py](examples/custom_agent.py).

The local Python API and command subprocess are trusted interfaces, not OS sandboxes. Run untrusted
programs in an isolated backend with scoped network access.

## Run an experiment

`EnvironmentHarness` builds a fresh environment and fresh agents for every session. A `Scenario[T]` freezes its validated input, optional JSON reference, and metadata. An experiment expands one frozen configuration across every scenario and trial under a shared concurrency limit.

```python
from environment_harness import EnvironmentHarness, Scenario
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment, SyntheticScenarioInput

harness = EnvironmentHarness(
    ".local/experiment",
    environment=SyntheticEnvironment,
    agents={"agent": SyntheticAgent},
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

[examples/typed_experiment.py](examples/typed_experiment.py) runs this end to end and prints the
experiment ID, every session ID, scenario and trial labels, deterministic seeds, and the viewer
path.

Use `experiment.start()`, `wait()`, `stop()`, and `resume()` for lifecycle control; standalone
sessions use `harness.start(...)` or `harness.run(...)`. The low-level coordinator, explicit
`ExperimentSpec`, and branching workflow live in `environment_harness.advanced`.

## Extend an environment

An environment package owns four ordinary Python methods: `initialize`, `observe`, `resolve`, and
`intervene`, plus a stable `EnvironmentSpec`. Add an `EnvironmentOperation` when the environment
also needs imperative work such as controlling a game, reading a simulator, or calling an engine.
Runtime clients and credentials stay on the Python object; only the operation's name, version, and
JSON configuration are frozen into evidence.

[examples/custom_environment_experiment.py](examples/custom_environment_experiment.py) is the
runnable version: it defines an operation, interleaves it with normal turns through the typed
`SessionRunner` seam, runs two scenarios across two trials, and records comparable metrics and
evidence-linked findings. Open the experiment path it prints — that page shows the configuration
shared by all four sessions, including the operation and scoring version.

![EnvironmentHarness experiment page showing frozen execution, environment, evaluation, and participant configuration](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-experiment.png)

A session's contextual navigation is Overview, Turns, Progression, Trajectory, and Configuration.
Overview summarizes versioned scores, uncertainty, and evidence-linked findings; Turns shows
operation receipts alongside agent and environment evidence; Progression plots recorded signals
across the selected turn range; Configuration holds the frozen experiment and lineage.

![EnvironmentHarness Progression page showing a recorded signal across eight turns](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-progression.png)

![EnvironmentHarness session Overview showing versioned metrics, uncertainty, and findings](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-harness-reports.png)

One operation class works in a standalone session or a grouped experiment — grouping is an
orchestration choice, not a different environment type. See
[Environment authoring](docs/AUTHORING.md), and the
[external simulator example](examples/external_environment_experiment.py) when your implementation
owns a long-lived process or engine connection.

## Inspect and export trajectories

Every session writes an append-only evidence journal. A trajectory is a portable, digest-bound view
of that journal, not a second writer. Pauses and resumes become causally linked execution segments,
and collection, execution, termination, and verified outcome are four independent states.

```python
trajectory = environment_session.trajectory()
snapshot = environment_session.snapshot()
for row in harness.sources().export_snapshot(snapshot.metadata.id):
    print(row)
```

The in-process SDK is a trusted local interface and needs no credential. Remote callers send only
an opaque bearer credential; the server resolves it to one of its fixed admin, viewer, or
participant policies. See [Authentication](docs/AUTHENTICATION.md).

The same interface imports hash-chained historical records from a namespaced external source.
Identical retries are idempotent, conflicting identities fail, and source health reports the
acknowledged position and hash, backlog, gaps, and capture failures. See
[Trajectories](docs/TRAJECTORIES.md) for the Python, CLI, and HTTP workflow.

[examples/trajectory_walkthrough.py](examples/trajectory_walkthrough.py) records a multi-segment
native session, imports a historical source, finalizes its independent states, freezes it, and
streams the snapshot.

Training datasets are immutable ordered snapshot selections and require complete, terminal,
training-entitled evidence with resolved reward chains. Trainer instances are injected locally;
the browser server never executes them. See [Frozen datasets and local training integrations](docs/TRAINING.md).

`decision.requested` and `decision.selected` are core record types with a fixed minimum payload and
zero, one, or many operation links. The selector runtime that produces them is separately owned and
is not part of this package. See [Decision runtime](docs/DECISION-RUNTIME.md).

## Scope of this SDK

This repository is the standalone public EnvironmentHarness SDK. It supplies the environment
boundary — sessions, evidence, trajectories, snapshots, datasets, and the authenticated local
service — and nothing above it. Private suppliers, marketplace behavior, catalog admission,
tenancy, billing, supplier settlement, hosted trainer orchestration, GPU allocation, deployment
state, credentials, and customer data are not in this package and are not SDK contracts.

An embedding product authenticates its own users and calls the in-process administrative seam;
EnvironmentHarness owns only the credential's policy and constraints. Product storage and APIs do
not become SDK contracts. See
[Authentication](docs/AUTHENTICATION.md) and [Compatibility](docs/COMPATIBILITY.md).

## Documentation

| Goal | Guide |
| --- | --- |
| Look up a resource, command, protocol, or status dimension | [Data models](https://github.com/kimpton-ai/environment-harness/blob/main/docs/DATA-MODELS.md) |
| Import, inspect, snapshot, and export trajectories | [Trajectories](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAJECTORIES.md) |
| Freeze datasets and invoke local training integrations | [Training](https://github.com/kimpton-ai/environment-harness/blob/main/docs/TRAINING.md) |
| Record decisions and understand the deferred selector runtime | [Decision runtime](https://github.com/kimpton-ai/environment-harness/blob/main/docs/DECISION-RUNTIME.md) |
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
