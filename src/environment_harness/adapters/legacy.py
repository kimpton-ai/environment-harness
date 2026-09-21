"""Versioned boundary for a serializable world-session.v1 implementation.

This adapter creates new environment sessions. It does not rewrite old stores,
manifests, hashes or evidence. Keep the original runtime to inspect old records.
"""

from copy import deepcopy

from ..contracts import EnvironmentSpec, Transition


class LegacyEnvironment:
    def __init__(self, implementation, *, version):
        legacy = implementation.spec.model_dump(mode="json")
        if legacy.get("protocol") != "world-session.v1" or not version or version == legacy["version"]:
            raise ValueError("legacy adaptation requires a distinct native version")
        self.implementation = implementation
        self.legacy_spec = deepcopy(legacy)
        self.spec = EnvironmentSpec.model_validate(
            {**legacy, "protocol": "environment-session.v1", "version": version}
        )

    def initialize(self, experiment):
        if experiment.environment != self.spec:
            raise ValueError("experiment does not match the adapted environment")
        return self.implementation.initialize(experiment)

    def observe(self, state, participant):
        return self.implementation.observe(state, participant)

    def resolve(self, state, actions, random, events):
        result = self.implementation.resolve(state, actions, random, events)
        return Transition.model_validate(result.model_dump(mode="json"))

    def intervene(self, state, changes):
        return self.implementation.intervene(state, changes)
