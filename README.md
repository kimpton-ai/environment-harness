# EnvironmentHarness

[![CI](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/kimpton-ai/environment-harness/graph/badge.svg?branch=main)](https://app.codecov.io/gh/kimpton-ai/environment-harness)
[![PyPI](https://img.shields.io/pypi/v/environment-harness.svg)](https://pypi.org/project/environment-harness/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Run agents in persistent shared environments, then inspect exactly what they observed, attempted, and changed.

EnvironmentHarness records participant-specific observations, actions, outcomes, checkpoints, score reports, and branch lineage as durable evidence. You provide the environment rules, agent programs, and grading method.

[Quickstart](#quickstart) · [Connect an agent](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) · [Implement an environment](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) · [Protocol](https://github.com/kimpton-ai/environment-harness/blob/main/docs/PROTOCOL.md)

## What you can do

- Run one or more agents against stateful environment rules.
- Give each participant a different private observation of the same shared state.
- Record observations, attempted actions, executed outcomes, scores, costs, and artifacts.
- Checkpoint an environment session, branch it with a declared intervention, and continue both sessions.
- Inspect timelines, compare related environment sessions, and export evidence as JSONL.

## Quickstart

Install Python 3.12 or later and the SDK with its local viewer:

```sh
python -m pip install "environment-harness[server]"
```

To install an editable source checkout instead, follow the [contributing guide](https://github.com/kimpton-ai/environment-harness/blob/main/CONTRIBUTING.md).

Create a synthetic environment session and open its evidence viewer:

```sh
environment-harness --store ./environment-sessions quickstart --turns 3
environment-harness --store ./environment-sessions serve --open
```

The demo runs two synthetic agents for three turns and ends with a shared counter of `6`. It needs no account, model API key, or paid service.

`serve` starts an authenticated API and read-only viewer at `http://127.0.0.1:8765`. It is available only on your computer and does not deploy or publish the environment session. Press `Ctrl+C` to stop it.

![EnvironmentHarness local evidence viewer showing original and branched environment sessions with a participant timeline](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-session-viewer.png)

The viewer answers three questions:

1. Which original or branched environment session am I inspecting?
2. What did each participant observe, attempt, and cause at each state revision?
3. What intervention and recorded results differ when I compare two sessions?

The screenshot uses the repository's richer [branch comparison example](https://github.com/kimpton-ai/environment-harness/blob/main/examples/branch_comparison.py). Its original total of `10` and branched total of `24` are synthetic counter values, not agent-quality scores or independent statistical results.

## Use your own agent

A Python agent implements `act(observation) -> dict`. Its action must match the environment's action schema, and its `implementation` must match the identifier registered in `AgentSpec`.

```python
from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runner import run


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

## Documentation

| Goal | Guide |
| --- | --- |
| Connect a Python agent, model integration, or JSON program | [Agent integration](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) |
| Implement and package environment rules | [Environment authoring](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) |
| Understand checkpoints, branches, and coordinated sessions | [Coordinated sessions](https://github.com/kimpton-ai/environment-harness/blob/main/docs/coordinated-sessions.md) |
| Use the authenticated HTTP API | [Protocol](https://github.com/kimpton-ai/environment-harness/blob/main/docs/PROTOCOL.md) |
| Use the TypeScript client | [TypeScript package](https://github.com/kimpton-ai/environment-harness/blob/main/packages/typescript/README.md) |
| Check adapter and isolation boundaries | [Adapters](https://github.com/kimpton-ai/environment-harness/blob/main/docs/ADAPTERS.md) |
| Check compatibility and release limits | [Compatibility](https://github.com/kimpton-ai/environment-harness/blob/main/docs/COMPATIBILITY.md) · [Release scope](https://github.com/kimpton-ai/environment-harness/blob/main/docs/STATUS.md) |

## Project

[Contributing](https://github.com/kimpton-ai/environment-harness/blob/main/CONTRIBUTING.md) · [Support](https://github.com/kimpton-ai/environment-harness/blob/main/SUPPORT.md) · [Security](https://github.com/kimpton-ai/environment-harness/blob/main/SECURITY.md) · [Changelog](https://github.com/kimpton-ai/environment-harness/blob/main/CHANGELOG.md) · [MIT license](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Report vulnerabilities privately through [SECURITY.md](https://github.com/kimpton-ai/environment-harness/blob/main/SECURITY.md); do not put credentials or private environment sessions in a public issue.
