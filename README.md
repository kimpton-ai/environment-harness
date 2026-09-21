# EnvironmentHarness

[![CI](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kimpton-ai/environment-harness/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/kimpton-ai/environment-harness/graph/badge.svg?branch=main)](https://app.codecov.io/gh/kimpton-ai/environment-harness)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-2ea44f.svg)](https://github.com/kimpton-ai/environment-harness/blob/main/LICENSE)

Run agents in persistent shared environments, then inspect exactly what they observed, attempted, and changed.

EnvironmentHarness is an MIT-licensed SDK for evaluations that unfold over time. It records participant-specific observations, actions, outcomes, checkpoints, score reports, and branch lineage as durable evidence.

[Install](#install) · [Try it locally](#try-it-locally) · [Connect an agent](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) · [Implement an environment](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AUTHORING.md) · [Protocol](https://github.com/kimpton-ai/environment-harness/blob/main/docs/PROTOCOL.md) · [Release scope](https://github.com/kimpton-ai/environment-harness/blob/main/docs/STATUS.md)

## What you can do

- Run one or more agents against stateful environment rules.
- Give each participant a different private observation of the same shared state.
- Record observations, attempted actions, executed outcomes, scores, costs, and artifacts.
- Checkpoint an environment session, branch it with a declared intervention, and continue both sessions.
- Inspect a timeline, compare related environment sessions, or export the evidence as JSONL.

You provide the environment rules, agent programs, and grading method. EnvironmentHarness coordinates the environment session and preserves the evidence.

```text
Your environment + agents + scorer
                 │
                 ▼
        EnvironmentSession
     records every state revision
                 │
                 ▼
       Python / CLI / local viewer
              / JSONL
```

## Install

Install the stable Python SDK from PyPI:

```sh
python -m pip install environment-harness
```

Add the local HTTP service and viewer when you need them:

```sh
python -m pip install "environment-harness[server]"
```

Release candidates use PEP 440 versions such as `0.2.3rc1`. Pip excludes prereleases from ordinary installs; test one by requesting its exact version or by opting in:

```sh
python -m pip install "environment-harness==0.2.3rc1"
python -m pip install --pre --upgrade environment-harness
```

## Try it locally

You need macOS or Linux, Python 3.12 or later, and [uv 0.12.0 or later](https://docs.astral.sh/uv/getting-started/installation/). The demo uses synthetic agents, so it needs no account, model API key, or paid service.

```sh
git clone --branch v0.2.3rc2 --depth 1 https://github.com/kimpton-ai/environment-harness.git
cd environment-harness
uv sync --extra server
uv run python examples/branch_comparison.py --store .local/branch-demo
uv run environment-harness --store .local/branch-demo serve --open
```

The last command serves something, but only on your computer:

- a local authenticated API at `http://127.0.0.1:8765`
- a read-only browser viewer for the evidence in `.local/branch-demo`

It does not deploy or publish the environment session. `--open` creates a short-lived local login link and opens the viewer. Press `Ctrl+C` in the terminal to stop the service.

### What the demo means

The example creates one original environment session with a shared counter. Alice and Bob each add `1` per turn:

1. After three turns, the original session's shared total is `6`.
2. The example saves a checkpoint and creates a separate branched session from it.
3. A declared intervention changes only the branched session's starting total from `6` to `20`.
4. Both sessions run for two more turns. The original finishes at `10`; the branch finishes at `24`.

The totals are values in the synthetic environment's shared state. They are not the number of agents, scores of agent quality, or independent statistical results. The example exists to demonstrate state, lineage, intervention, and recorded evidence.

### What the viewer shows

The viewer is an evidence inspector, not a control panel:

![EnvironmentHarness local evidence viewer showing original and branched environment sessions with a participant timeline](https://raw.githubusercontent.com/kimpton-ai/environment-harness/main/docs/assets/environment-session-viewer.png)

1. The **Environment sessions** list lets you open the original or branched session. Check two boxes to compare them.
2. **Timeline** groups evidence by state revision. Each participant row shows the observation delivered to that participant, the action it attempted, and the outcome the environment executed.
3. **Scores and findings** shows reports submitted by your scorer. In this demo, `synthetic total` is just the final counter value.
4. **Environment comparison** shows what the sessions share, the intervention applied to the branch, and their recorded results.

The viewer never changes an environment session. Use the Python SDK or CLI for checkpoint, branch, resume, cancel, and execution operations.

## Use your own agent

Run the included external JSON program through the command-agent boundary:

```sh
uv run python examples/custom_agent.py --store .local/custom-agent
uv run environment-harness --store .local/custom-agent serve --open
```

A Python agent implements `act(observation) -> dict`. An external program can instead read one JSON observation from stdin and write one JSON action to stdout. Your integration keeps ownership of prompts, model providers, tools, credentials, and spending limits.

See [Connect an agent](https://github.com/kimpton-ai/environment-harness/blob/main/docs/AGENT-INTEGRATION.md) for the complete contract and [custom_agent.py](https://github.com/kimpton-ai/environment-harness/blob/main/examples/custom_agent.py) for a working example.

## Use the Python API

```python
from environment_harness import AgentSpec, EnvironmentSession, EvidenceStore, ExperimentSpec, Principal
from environment_harness.fixtures import SyntheticAgent, SyntheticEnvironment
from environment_harness.runner import run

environment = SyntheticEnvironment()
session = EnvironmentSession(EvidenceStore(".local/experiment"), environment)
researcher = Principal(tenant="local", subject="researcher", role="researcher")
experiment = ExperimentSpec(
    environment=environment.spec,
    participants=(
        AgentSpec(id="alice", implementation="synthetic-agent@1", policy_version="1"),
    ),
)

environment_session = session.create(experiment, researcher)
run(
    session,
    environment_session["id"],
    researcher,
    {"alice": SyntheticAgent()},
    turns=5,
)
```

The local Python API is a trusted embedding interface. A local command subprocess is also not an operating-system security sandbox. Run hostile programs in an isolated backend with explicitly scoped network access.

## Find the right guide

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

Report vulnerabilities privately through [SECURITY.md](https://github.com/kimpton-ai/environment-harness/blob/main/SECURITY.md). Do not put credentials or private environment sessions in a public issue.
