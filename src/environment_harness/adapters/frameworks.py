"""Compatibility boundaries for Inspect and the bounded Verifiers legacy API."""

import re
from importlib.metadata import version

from ..errors import Unsupported


def inspect_agent(program):
    """Adapt an async program taking an Inspect bridge and AgentState."""
    from inspect_ai.agent import agent, agent_bridge

    @agent
    def adapted():
        async def execute(state):
            async with agent_bridge(state) as bridge:
                await program(bridge, state)
            return state

        return execute

    return adapted()


class VerifiersRolloutConsumer:
    supported = ">=0.3.1,<0.4"

    def __init__(self, environment):
        installed = version("verifiers")
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)", installed)
        numbers = tuple(int(value) for value in match.groups()) if match is not None else None
        if numbers is None or not ((0, 3, 1) <= numbers < (0, 4, 0)):
            raise Unsupported(f"Verifiers bridge targets {self.supported}; installed {installed}")
        self.environment = environment

    async def rollout(self, input, client, model, sampling_args=None):
        return await self.environment.rollout(
            input=input, client=client, model=model, sampling_args=sampling_args
        )

    def training_rows(self, store, environment, who):
        trajectory = self.trajectory_resource(store, environment, who)
        observations = {}
        for record in trajectory["status"]["records"]:
            if record["type"] in ("observation.delivered", "environment.observation"):
                observations[record["data"].get("id", record["id"])] = record
            if record["type"] not in ("action.executed", "agent.action"):
                continue
            data = record["data"]
            observation = observations.get(data.get("observation_id"))
            yield {
                "prompt": None if observation is None else observation["data"].get("payload"),
                "completion": data.get("outcome", data.get("payload")),
                "reward": data.get("reward"),
                "metadata": {
                    "trajectory_id": trajectory["metadata"]["id"],
                    "trajectory_digest": trajectory["status"]["trajectoryDigest"],
                    "record_id": record["id"],
                    "participant": record["participant"],
                    "policy_version": data.get("policy_version"),
                    "terminated": data.get("terminated", False),
                    "truncated": data.get("truncated", False),
                    "reason": data.get("reason"),
                },
                "tokens": None,
            }

    @staticmethod
    def trajectory_resource(store, environment, who):
        from ..trajectories import TrajectoryRepository

        return TrajectoryRepository(store).get(environment, who).model_dump(mode="json", by_alias=True)
