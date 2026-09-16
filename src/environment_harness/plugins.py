"""Explicit optional plugin discovery and compatibility diagnostics."""

from importlib.metadata import PackageNotFoundError, entry_points, version

from .errors import Unsupported
from .fixtures import SyntheticEnvironment


def discover():
    return [
        {"name": entry.name, "distribution": entry.dist.name if entry.dist else None, "target": entry.value}
        for entry in entry_points(group="environment_harness.environments")
    ]


def environment(name, **config):
    if name == "synthetic-protocol":
        return SyntheticEnvironment(**config)
    matches = list(entry_points(group="environment_harness.environments", name=name))
    if len(matches) != 1:
        raise Unsupported("install one environment plugin with the requested name")
    return matches[0].load()(**config)


def doctor():
    dependencies = {}
    for package in (
        "environment-harness",
        "pydantic",
        "fastapi",
        "modal",
        "psycopg",
        "pettingzoo",
        "inspect-ai",
        "openenv-core",
        "verifiers",
        "prime",
    ):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    return {
        "protocol": "environment-session.v1",
        "dependencies": dependencies,
        "plugins": discover(),
        "hosted_qualification": "not asserted by installation",
        "private_suppliers": "installed separately",
    }
