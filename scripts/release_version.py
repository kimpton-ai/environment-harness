"""Prepare a reviewed release change and validate its maintainer-created tag."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ReleaseError(RuntimeError):
    """Release metadata is missing, inconsistent, or unsafe to advance."""


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise ReleaseError(f"version is not strict three-component SemVer: {value!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def current_version(root: Path = ROOT) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())
    package = json.loads((root / "packages/typescript/package.json").read_text())
    package_lock = json.loads((root / "packages/typescript/package-lock.json").read_text())
    package_init = (root / "src/environment_harness/__init__.py").read_text()
    exported = re.search(r'^__version__\s*=\s*"([^"]+)"$', package_init, re.MULTILINE)
    versions = {
        project.get("project", {}).get("version"),
        package.get("version"),
        package_lock.get("version"),
        package_lock.get("packages", {}).get("", {}).get("version"),
        exported.group(1) if exported else None,
    }
    if len(versions) != 1:
        raise ReleaseError(f"release versions differ: {sorted(repr(item) for item in versions)}")
    version = versions.pop()
    if not isinstance(version, str):
        raise ReleaseError("release version is missing")
    parse_version(version)
    return version


def bumped_version(version: str, bump: str) -> str:
    major, minor, patch = parse_version(version)
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "patch":
        patch += 1
    else:
        raise ReleaseError(f"unsupported version bump: {bump!r}")
    return f"{major}.{minor}.{patch}"


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    updated, count = re.subn(pattern, replacement, path.read_text(), count=1, flags=re.MULTILINE | re.DOTALL)
    if count != 1:
        raise ReleaseError(f"expected exactly one release version in {path}")
    path.write_text(updated)


def _update_json_versions(path: Path, version: str, *, lockfile: bool = False) -> None:
    document = json.loads(path.read_text())
    document["version"] = version
    if lockfile:
        root_package = document.get("packages", {}).get("")
        if not isinstance(root_package, dict):
            raise ReleaseError("npm lockfile root package is missing")
        root_package["version"] = version
    path.write_text(json.dumps(document, indent=2) + "\n")


def prepare_release(root: Path, bump: str, released_on: date | None = None) -> str:
    previous = current_version(root)
    version = bumped_version(previous, bump)
    released_on = released_on or date.today()

    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text()
    heading = re.search(r"(?m)^## Unreleased[ \t]*$", changelog)
    if not heading:
        raise ReleaseError("CHANGELOG.md lacks an Unreleased section")
    next_heading = re.search(r"(?m)^## ", changelog[heading.end() :])
    section_end = heading.end() + (next_heading.start() if next_heading else len(changelog))
    if not changelog[heading.end() : section_end].strip():
        raise ReleaseError("CHANGELOG.md Unreleased section is empty")
    changelog = (
        changelog[: heading.end()]
        + f"\n\n## {version} - {released_on.isoformat()}"
        + changelog[heading.end() :]
    )
    changelog_path.write_text(changelog)

    _replace_once(
        root / "pyproject.toml",
        rf'(^\[project\]\n(?:(?!^\[).)*?^version = "){re.escape(previous)}("$)',
        rf"\g<1>{version}\g<2>",
    )
    _replace_once(
        root / "src/environment_harness/__init__.py",
        rf'(^__version__\s*=\s*"){re.escape(previous)}("$)',
        rf"\g<1>{version}\g<2>",
    )
    _replace_once(
        root / "uv.lock",
        rf'(^\[\[package\]\]\n(?:(?!^\[\[package\]\]).)*?^name = "environment-harness"\n^version = "){re.escape(previous)}("$)',
        rf"\g<1>{version}\g<2>",
    )
    _replace_once(
        root / "README.md",
        rf"(--branch v){re.escape(previous)}(\s)",
        rf"\g<1>{version}\g<2>",
    )
    _update_json_versions(root / "packages/typescript/package.json", version)
    _update_json_versions(root / "packages/typescript/package-lock.json", version, lockfile=True)

    if current_version(root) != version:
        raise ReleaseError("release preparation left inconsistent versions")
    return version


def select_release_tag(version: str, tags: list[str], changelog: str) -> str | None:
    current = parse_version(version)
    heading = re.compile(rf"(?m)^## (?:\[{re.escape(version)}\]|{re.escape(version)})(?:\s|$)")
    if not heading.search(changelog):
        raise ReleaseError(f"CHANGELOG.md lacks a {version} release section")

    released = {}
    for tag in tags:
        if not tag.startswith("v") or not SEMVER.fullmatch(tag[1:]):
            continue
        released[parse_version(tag[1:])] = tag
    if current in released:
        return None
    if any(item > current for item in released):
        newest = released[max(released)]
        raise ReleaseError(f"metadata version {version} is older than existing release {newest}")
    return f"v{version}"


def detect_release_tag(root: Path = ROOT) -> str | None:
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return select_release_tag(current_version(root), tags, (root / "CHANGELOG.md").read_text())


def validate_release_tag(release_tag: str, root: Path = ROOT) -> None:
    version = current_version(root)
    if release_tag != f"v{version}":
        raise ReleaseError(f"release tag {release_tag!r} does not match metadata version v{version}")
    select_release_tag(version, [], (root / "CHANGELOG.md").read_text())


def _write_outputs(path: Path | None, values: dict[str, str]) -> None:
    if path is None:
        print(json.dumps(values, sort_keys=True))
        return
    with path.open("a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--bump", required=True, choices=("patch", "minor", "major"))
    prepare.add_argument("--github-output", type=Path)
    detect = subparsers.add_parser("detect")
    detect.add_argument("--github-output", type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--release-tag", required=True)
    args = parser.parse_args()

    if args.command == "prepare":
        pending = detect_release_tag(ROOT)
        if pending:
            raise ReleaseError(f"current metadata has not been released as {pending}")
        version = prepare_release(ROOT, args.bump)
        _write_outputs(args.github_output, {"version": version, "tag": f"v{version}"})
    elif args.command == "detect":
        tag = detect_release_tag(ROOT)
        _write_outputs(
            args.github_output,
            {"create": "true" if tag else "false", "tag": tag or ""},
        )
    else:
        validate_release_tag(args.release_tag)


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as error:
        raise SystemExit(f"release preparation failed: {error}") from error
