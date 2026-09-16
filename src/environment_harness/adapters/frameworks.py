"""Compatibility boundaries for Inspect and installed Verifiers 0.1.14."""

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
    supported = "0.1.14"

    def __init__(self, environment):
        installed = version("verifiers")
        if installed != self.supported:
            raise Unsupported(f"Verifiers bridge targets {self.supported}; installed {installed}")
        self.environment = environment

    async def rollout(self, input, client, model, sampling_args=None):
        return await self.environment.rollout(
            input=input, client=client, model=model, sampling_args=sampling_args
        )

    def training_rows(self, store, environment, who):
        from ..evaluation import rollouts

        for row in rollouts(store, environment, who):
            yield {
                "prompt": row["observation"]["payload"],
                "completion": row["action"]["payload"],
                "reward": row["reward"],
                "metadata": {
                    k: row[k]
                    for k in (
                        "environment",
                        "lineage",
                        "participant",
                        "policy_version",
                        "terminated",
                        "truncated",
                        "reason",
                        "delayed_rewards",
                    )
                },
                "tokens": None,
            }
