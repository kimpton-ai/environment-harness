"""Optional existing-environment adapters. Persistence guarantees are explicit."""

from ..contracts import Capabilities, EnvironmentSpec, Transition
from ..errors import Conflict, Unsupported
from ..store import digest, uid


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if hasattr(value, "item"):
        return value.item()
    return value


class EphemeralAdapter:
    """One native session per adapter. Retains one transition receipt for commit retry."""

    def __init__(self):
        self.epoch, self.initialized, self.last_key, self.last_result = uid(), False, None, None
        self.turn = 0

    def _initialize(self):
        if self.initialized:
            raise Conflict("native adapter instance is already assigned to an environment")
        self.initialized = True

    def _resolve(self, state, actions, perform):
        if state["epoch"] != self.epoch:
            raise Unsupported("native session was lost; recorded replay remains available")
        key = digest({"state": state, "actions": actions})
        if key == self.last_key:
            return self.last_result
        if state["turn"] != self.turn:
            raise Conflict("native adapter outcome is ambiguous")
        # Mark transition as uncertain before calling an imperative upstream API.
        self.turn += 1
        result = perform()
        self.last_key, self.last_result = key, result
        return result

    def intervene(self, state, changes):
        raise Unsupported("upstream environment has no advertised counterfactual state hooks")

    def observe(self, state, participant):
        return state["observations"][participant]


class PettingZooParallel(EphemeralAdapter):
    def __init__(self, environment, *, name, version):
        super().__init__()
        self.native = environment
        self.spec = EnvironmentSpec(
            id=name,
            version=version,
            implementation=f"pettingzoo-parallel:{name}@{version}",
            scheduling="simultaneous",
            capabilities=Capabilities(),
            purposes=("evaluation", "training"),
            action_schema={
                "type": "object",
                "properties": {"action": {}},
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def initialize(self, experiment):
        self._initialize()
        observations, infos = self.native.reset(seed=experiment.seed)
        if set(observations) != {p.id for p in experiment.participants}:
            raise Conflict("participants must match upstream agent IDs")
        return {
            "epoch": self.epoch,
            "turn": 0,
            "observations": {
                p: {"observation": plain(o), "info": plain(infos.get(p, {}))} for p, o in observations.items()
            },
        }

    def resolve(self, state, actions, random, events):
        def perform():
            observations, rewards, terminations, truncations, infos = self.native.step(
                {p: a["action"] for p, a in actions.items() if a is not None and p in self.native.agents}
            )
            return Transition(
                state={
                    "epoch": self.epoch,
                    "turn": self.turn,
                    "observations": {
                        p: {"observation": plain(observations.get(p)), "info": plain(infos.get(p, {}))}
                        for p in state["observations"]
                    },
                },
                outcomes={
                    p: {"terminated": bool(terminations.get(p)), "truncated": bool(truncations.get(p))}
                    for p in actions
                },
                rewards=plain(rewards),
                terminated=not self.native.agents and not any(truncations.values()),
                truncated=not self.native.agents and any(truncations.values()),
            )

        return self._resolve(state, actions, perform)


class PettingZooAEC(EphemeralAdapter):
    def __init__(self, environment, *, name, version):
        super().__init__()
        self.native = environment
        self.spec = EnvironmentSpec(
            id=name,
            version=version,
            implementation=f"pettingzoo-aec:{name}@{version}",
            scheduling="sequential",
            capabilities=Capabilities(),
            purposes=("evaluation", "training"),
        )

    def _observations(self, participants):
        return {
            p: {
                "observation": plain(self.native.observe(p)) if p in self.native.agents else None,
                "terminated": bool(self.native.terminations.get(p, False)),
                "truncated": bool(self.native.truncations.get(p, False)),
            }
            for p in participants
        }

    def initialize(self, experiment):
        self._initialize()
        self.native.reset(seed=experiment.seed)
        participants = [p.id for p in experiment.participants]
        if set(participants) != set(self.native.agents) or participants[0] != self.native.agent_selection:
            raise Conflict("participants and first actor must match the native AEC session")
        return {"epoch": self.epoch, "turn": 0, "observations": self._observations(participants)}

    def resolve(self, state, actions, random, events):
        def perform():
            actor = self.native.agent_selection
            if set(actions) != {actor}:
                raise Conflict("native AEC actor mismatch")
            dead = self.native.terminations[actor] or self.native.truncations[actor]
            self.native.step(None if dead else actions[actor]["action"])
            done = not self.native.agents
            return Transition(
                state={
                    "epoch": self.epoch,
                    "turn": self.turn,
                    "observations": self._observations(state["observations"]),
                },
                rewards=plain(self.native.rewards),
                terminated=done,
                next_actor=self.native.agent_selection if not done else actor,
            )

        return self._resolve(state, actions, perform)


class OpenEnvSession(EphemeralAdapter):
    def __init__(self, sync_client, *, name, version):
        super().__init__()
        self.native = sync_client
        self.spec = EnvironmentSpec(
            id=name,
            version=version,
            implementation=f"openenv:{name}@{version}",
            scheduling="sequential",
            capabilities=Capabilities(),
        )

    def initialize(self, experiment):
        self._initialize()
        if len(experiment.participants) != 1:
            raise Unsupported("ordinary OpenEnv sessions admit one participant")
        self.participant = experiment.participants[0].id
        result = self.native.reset(seed=experiment.seed)
        return {
            "epoch": self.epoch,
            "turn": 0,
            "observations": {self.participant: {"observation": plain(result.observation)}},
        }

    def resolve(self, state, actions, random, events):
        def perform():
            result = self.native.step(actions[self.participant])
            return Transition(
                state={
                    "epoch": self.epoch,
                    "turn": self.turn,
                    "observations": {self.participant: {"observation": plain(result.observation)}},
                },
                rewards={self.participant: float(result.reward or 0)},
                terminated=bool(result.done),
            )

        return self._resolve(state, actions, perform)
