import math

import pytest

from environment_harness import AgentSpec, EvidenceStore, ExperimentSpec
from environment_harness.access import _AccessContext
from environment_harness.adapters.programs import InstrumentedModel
from environment_harness.contracts import RunPolicy
from environment_harness.errors import Conflict
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runner import run
from environment_harness.runtime import _SessionRuntime


def test_instrumented_model_correlates_calls_to_durable_agent_work(tmp_path):
    store = EvidenceStore(tmp_path)
    environment = SyntheticEnvironment()
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="model-agent", policy_version="1"),),
            policy=RunPolicy(max_turns=1),
        ),
        researcher,
    )["id"]
    agent_principal = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session=environment_id,
        participant="alice",
    )

    class Agent:
        implementation = "model-agent"

        def __init__(self):
            self.model = InstrumentedModel(
                store,
                environment_id,
                agent_principal,
                lambda _request: {
                    "text": "one",
                    "token_ids": [1],
                    "logprobs": [-0.25],
                    "usage": {"output_tokens": 1},
                    "finish_reason": "stop",
                    "action": {"value": 1},
                },
                model="synthetic-model",
                tokenizer="synthetic-tokenizer",
                renderer="synthetic-renderer",
                seed=7,
            )

        def act(self, observation):
            return self.model.call({"observation": observation})["action"]

    run(session, environment_id, researcher, {"alice": Agent()}, turns=1)
    events = list(store.replay(environment_id, researcher))
    dispatched = next(event for event in events if event["kind"] == "agent.dispatched")
    request = next(event for event in events if event["kind"] == "model.request")
    response = next(event for event in events if event["kind"] == "model.response")

    assert request["payload"]["call_id"] == response["payload"]["call_id"]
    assert request["payload"]["correlation"] == {
        "agent_operation_id": dispatched["payload"]["operation_id"],
        "observation_id": next(
            event["payload"]["observation_id"] for event in events if event["kind"] == "action.executed"
        ),
        "participant": "alice",
        "generation": 0,
        "revision": 0,
    }
    assert response["payload"]["validation"] == "validated"


@pytest.mark.parametrize(
    "response",
    [
        {"token_ids": [1], "logprobs": []},
        {"token_ids": [True], "logprobs": [-0.1]},
        {"token_ids": [1], "logprobs": [0.1]},
        {"token_ids": [1], "logprobs": [math.nan]},
    ],
)
def test_instrumented_model_rejects_invalid_token_evidence(tmp_path, response):
    store = EvidenceStore(tmp_path)
    environment = SyntheticEnvironment()
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="external", policy_version="1"),),
        ),
        researcher,
    )["id"]
    principal = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session=environment_id,
        participant="alice",
    )
    model = InstrumentedModel(store, environment_id, principal, lambda _request: response)

    with pytest.raises(Conflict, match="inference evidence"):
        model.call("prompt")


def test_instrumented_model_spills_oversized_detail_to_participant_artifacts(tmp_path):
    store = EvidenceStore(tmp_path)
    environment = SyntheticEnvironment()
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="external", policy_version="1"),),
            policy=RunPolicy(max_event_bytes=4096, max_artifact_bytes=200_000),
        ),
        researcher,
    )["id"]
    principal = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session=environment_id,
        participant="alice",
    )
    request = {"prompt": "r" * 10_000}
    response = {
        "text": "s" * 10_000,
        "token_ids": list(range(2_000)),
        "logprobs": [-0.1] * 2_000,
        "usage": {"output_tokens": 2_000},
        "finish_reason": "length",
    }

    assert (
        InstrumentedModel(
            store,
            environment_id,
            principal,
            lambda _request: response,
            capture_content=True,
        ).call(request)
        == response
    )

    events = list(store.replay(environment_id, researcher))
    model_request = next(event for event in events if event["kind"] == "model.request")
    model_response = next(event for event in events if event["kind"] == "model.response")
    artifacts = [event for event in events if event["kind"] == "artifact"]
    assert len(artifacts) == 2
    assert model_request["payload"]["rendered_request"] is None
    assert model_request["payload"]["detail_artifact"]["purpose"] == "inference.request"
    assert model_response["payload"]["response"] is None
    assert model_response["payload"]["token_ids"] is None
    assert model_response["payload"]["logprobs"] is None
    assert model_response["payload"]["detail_artifact"]["purpose"] == "inference.response"
    with store.transaction() as database:
        rows = database.execute(
            "SELECT audience,media_type,size FROM artifacts WHERE environment=? ORDER BY rowid",
            (environment_id,),
        ).fetchall()
    assert [row["audience"] for row in rows] == ['["alice"]', '["alice"]']
    assert all(row["media_type"] == "application/json" and row["size"] > 4096 for row in rows)


def test_instrumented_model_rejects_unserializable_requests_and_non_mapping_responses(tmp_path):
    store = EvidenceStore(tmp_path)
    environment = SyntheticEnvironment()
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="external", policy_version="1"),),
        ),
        researcher,
    )["id"]
    principal = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session=environment_id,
        participant="alice",
    )
    model = InstrumentedModel(store, environment_id, principal, lambda _request: "not-a-mapping")

    with pytest.raises(Conflict, match="request"):
        model.call({"not-json": object()})
    with pytest.raises(Conflict, match="response"):
        model.call({"prompt": "valid"})


def test_instrumented_model_rejects_an_oversized_artifact_summary(tmp_path):
    store = EvidenceStore(tmp_path)
    environment = SyntheticEnvironment()
    session = _SessionRuntime(store, environment)
    researcher = _AccessContext(tenant="tenant", subject="researcher", policy="trusted-local")
    environment_id = session.create(
        ExperimentSpec(
            environment=environment.spec,
            participants=(AgentSpec(id="alice", implementation="external", policy_version="1"),),
            policy=RunPolicy(max_event_bytes=4096, max_artifact_bytes=200_000),
        ),
        researcher,
    )["id"]
    principal = _AccessContext(
        tenant="tenant",
        subject="alice",
        policy="participant",
        session=environment_id,
        participant="alice",
    )
    model = InstrumentedModel(
        store,
        environment_id,
        principal,
        lambda _request: {"text": "ok"},
        model="m" * 10_000,
        capture_content=True,
    )

    with pytest.raises(Conflict, match="summary exceeds"):
        model.call({"prompt": "r" * 10_000})
