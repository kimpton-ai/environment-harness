# EnvironmentHarness

[![CI](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/kimpton-ai/environment-harness/graph/badge.svg?branch=main)](https://app.codecov.io/gh/kimpton-ai/environment-harness)
[![PyPI](https://img.shields.io/pypi/v/environment-harness.svg)](https://pypi.org/project/environment-harness/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Run agents in persistent shared environments, then inspect exactly what they observed, attempted, and changed.

EnvironmentHarness records participant-specific observations, actions, outcomes, checkpoints, score reports, and branch lineage as durable evidence. You provide the environment rules, agent programs, and grading method.

[Quickstart](#quickstart) · [Connect an agent](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) · [Implement an environment](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) · [API reference](https://github.com/kimpton-ai/environment-harness/blob/main/docs/API-REFERENCE.md) · [Protocol](https://github.com/kimpton-ai/environment-harness/blob/main/docs/PROTOCOL.md)

## What you can do

- Run one or more agents against stateful environment rules.
- Give each participant a different private observation of the same shared state.
- Record observations, attempted actions, executed outcomes, scores, costs, and artifacts.
- Checkpoint an environment session, branch it with a declared intervention, and continue both sessions.
- Inspect timelines, compare related environment sessions, and export evidence as JSONL.
- Validate typed scenarios and run related scenario/trial experiments concurrently.

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

The demo creates two named experiments and three named standalone environment sessions. Five frozen scenarios expand across two trials, so Home shows 10 grouped environment sessions with labels such as `Refund request · Trial 1`. The customer-support experiment uses the requested turn budget while the policy-boundary experiment uses half that budget (with a minimum of one); scenarios within each experiment correctly share its frozen budget. The standalone reviews also vary their participant rosters and recorded turn counts. Every grouped session records a deterministic synthetic comparison report with a final total, cumulative reward, and executed-action count. Each standalone review records versioned score reports, one intentionally blocked out-of-schema attempt with an attributed finding, a checkpoint, and a small public artifact. Together they exercise hierarchy, filtering, unequal-length comparison, Turns, Progression, Reports, and structured Activity without needing an account, model API key, or paid service. None of the synthetic values measure model quality or safety.

Each demo run finishes after the requested turns, so Home reports it as `Completed`. Standalone rows show the number of turns actually recorded; safety limits such as maximum turns remain in the session Overview instead of appearing as unfinished progress.

`serve` starts an authenticated API and read-only viewer at `http://127.0.0.1:8765`. The loopback viewer receives local researcher access automatically; `--open` only opens it in your browser. API clients still use an explicit credential from `environment-harness token`. The server is available only on your computer and does not deploy or publish the environment session. Press `Ctrl+C` to stop it.

The running service publishes its generated OpenAPI document at `/openapi.json` and interactive reference at `/docs`. The repository checks the committed [`contracts/openapi.json`](contracts/openapi.json) and versioned JSON Schemas in [`contracts/`](contracts/) for drift. [`docs/API-REFERENCE.md`](docs/API-REFERENCE.md) documents endpoints and examples; [`docs/PROTOCOL.md`](docs/PROTOCOL.md) defines the authority, lifecycle, activity-stream, recovery, and evidence semantics that OpenAPI alone cannot express.

![EnvironmentHarness local evidence viewer showing original and branched environment sessions with a participant timeline](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-session-viewer.png)

The viewer answers three questions:

1. Which original or branched environment session am I inspecting?
2. What did each participant observe, attempt, and cause at each state revision?
3. What intervention and recorded results differ when I compare two sessions?

The screenshot uses the repository's richer [branch comparison example](https://github.com/kimpton-ai/environment-harness/blob/main/examples/branch_comparison.py). Its original total of `10` and branched total of `24` are synthetic counter values, not agent-quality scores or independent statistical results.

## Use your own agent

A Python agent implements `act(observation) -> dict`. Its action must match the environment's action schema, and its `implementation` must match the identifier registered in `AgentSpec`.

```python
from environment_harness import AgentSpec, EvidenceStore, Principal
from environment_harness.advanced import EnvironmentSession, ExperimentSpec, run
from environment_harness.fixtures import SyntheticEnvironment


class IncrementAgent:
    implementation = "increment-agent@1"

    def act(self, observation):
        return {"value": 1}


environment = SyntheticEnvironment()
session = EnvironmentSession(EvidenceStore(".local/experiment"), environment)
researcher = Principal(tenant="local", subject="researcher", role="researcher")
experiment = ExperimentSpec(
    environment=environment.spec,
    participants=(
        AgentSpec(id="alice", implementation="increment-agent@1", policy_version="1"),
    ),
)

environment_session = session.create(experiment, researcher)
result = run(
    session,
    environment_session["id"],
    researcher,
    {"alice": IncrementAgent()},
    turns=5,
)
print(result["revision"])
```

This prints `5`. Your integration keeps ownership of prompts, model providers, tools, credentials, and spending limits. External JSON programs can use `CommandAgent`; see the [complete source example](https://github.com/kimpton-ai/environment-harness/blob/main/examples/custom_agent.py).

The local Python API and command subprocess are trusted interfaces, not operating-system security sandboxes. Run hostile programs in an isolated backend with explicitly scoped network access.

## Run typed scenarios concurrently

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

## Documentation

| Goal | Guide |
| --- | --- |
| Connect a Python agent, model integration, or JSON program | [Agent integration](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) |
| Implement and package environment rules | [Environment authoring](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) |
| Understand checkpoints, branches, and coordinated sessions | [Coordinated sessions](https://github.com/kimpton-ai/environment-harness/blob/main/docs/coordinated-sessions.md) |
| Run environments behind a trusted supervisor | [Remote workers and external agents](https://github.com/kimpton-ai/environment-harness/blob/main/docs/REMOTE-WORKERS.md) |
| Add bounded native, browser, or desktop actions | [Motor control](https://github.com/kimpton-ai/environment-harness/blob/main/docs/motor-control.md) |
| Use the authenticated HTTP API | [API reference](https://github.com/kimpton-ai/environment-harness/blob/main/docs/API-REFERENCE.md) · [Protocol semantics](https://github.com/kimpton-ai/environment-harness/blob/main/docs/PROTOCOL.md) |
| Use the TypeScript client | [TypeScript package](https://github.com/kimpton-ai/environment-harness/blob/main/packages/typescript/README.md) |
| Check adapter and isolation boundaries | [Adapters](https://github.com/kimpton-ai/environment-harness/blob/main/docs/ADAPTERS.md) |
| Check compatibility and release limits | [Compatibility](https://github.com/kimpton-ai/environment-harness/blob/main/docs/COMPATIBILITY.md) · [Release scope](https://github.com/kimpton-ai/environment-harness/blob/main/docs/STATUS.md) |
| Install, run, and evaluate deployment options | [Deployment](https://github.com/kimpton-ai/environment-harness/blob/main/docs/DEPLOYMENT.md) |
| Maintain the packaged browser UI | [Viewer maintenance](https://github.com/kimpton-ai/environment-harness/blob/main/docs/VIEWER-MAINTENANCE.md) |
| Prepare and publish a release | [Release process](https://github.com/kimpton-ai/environment-harness/blob/main/docs/RELEASING.md) |

## Project

[Contributing](https://github.com/kimpton-ai/environment-harness/blob/main/CONTRIBUTING.md) · [Support](https://github.com/kimpton-ai/environment-harness/blob/main/SUPPORT.md) · [Security](https://github.com/kimpton-ai/environment-harness/blob/main/SECURITY.md) · [Changelog](https://github.com/kimpton-ai/environment-harness/blob/main/CHANGELOG.md) · [MIT license](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Optional [motor control](docs/motor-control.md) adds bounded native, browser and desktop skills with durable receipts and an optional Jev selector. Direct execution remains the default.

Report vulnerabilities privately through [SECURITY.md](https://github.com/kimpton-ai/environment-harness/blob/main/SECURITY.md). Do not put credentials or private environment sessions in a public issue.
