"""Prepare a reviewed release change and validate its maintainer-created tag."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PEP440_RELEASE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:(a|b|rc)([1-9][0-9]*))?$"
)
STAGE_ORDER = {"a": 0, "b": 1, "rc": 2, None: 3}
STAGE_NAMES = {"alpha": "a", "beta": "b", "rc": "rc"}
NPM_STAGE_NAMES = {"a": "alpha", "b": "beta", "rc": "rc"}
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class ReleaseError(RuntimeError):
    """Release metadata is missing, inconsistent, or unsafe to advance."""


@dataclass(frozen=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int
    stage: str | None = None
    serial: int | None = None

    @property
    def base(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def key(self) -> tuple[int, int, int, int, int]:
        return (
            self.major,
            self.minor,
            self.patch,
            STAGE_ORDER[self.stage],
            self.serial or 0,
        )


def parse_version(value: str) -> ReleaseVersion:
    match = PEP440_RELEASE.fullmatch(value)
    if not match:
        raise ReleaseError(f"version is not a supported PEP 440 release: {value!r}")
    return ReleaseVersion(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        match.group(4),
        int(match.group(5)) if match.group(5) else None,
    )


def npm_version(version: str) -> str:
    parsed = parse_version(version)
    if parsed.stage is None:
        return parsed.base
    return f"{parsed.base}-{NPM_STAGE_NAMES[parsed.stage]}.{parsed.serial}"


def current_version(root: Path = ROOT) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())
    package = json.loads((root / "packages/typescript/package.json").read_text())
    package_lock = json.loads((root / "packages/typescript/package-lock.json").read_text())
    package_init = (root / "src/environment_harness/__init__.py").read_text()
    package_readme = (root / "packages/typescript/README.md").read_text()
    status = (root / "docs/STATUS.md").read_text()
    exported = re.search(r'^__version__\s*=\s*"([^"]+)"$', package_init, re.MULTILINE)
    packaged_clients = set(
        re.findall(
            r"environment-harness-client-([0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha|beta|rc)\.[0-9]+)?)\.tgz",
            package_readme,
        )
    )
    documented_status = re.search(
        r"^EnvironmentHarness ([0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?) focuses",
        status,
        re.MULTILINE,
    )
    python_versions = {
        project.get("project", {}).get("version"),
        exported.group(1) if exported else None,
        documented_status.group(1) if documented_status else None,
    }
    if len(python_versions) != 1:
        raise ReleaseError(
            f"Python release versions differ: {sorted(repr(item) for item in python_versions)}"
        )
    version = python_versions.pop()
    if not isinstance(version, str):
        raise ReleaseError("release version is missing")
    parse_version(version)
    expected_npm = npm_version(version)
    npm_versions = {
        package.get("version"),
        package_lock.get("version"),
        package_lock.get("packages", {}).get("", {}).get("version"),
        *packaged_clients,
    }
    if not packaged_clients:
        npm_versions.add(None)
    if npm_versions != {expected_npm}:
        raise ReleaseError(
            f"npm release versions differ from {expected_npm}: {sorted(repr(item) for item in npm_versions)}"
        )
    return version


def _bumped_release_line(parsed: ReleaseVersion, bump: str) -> str:
    major, minor, patch = parsed.major, parsed.minor, parsed.patch
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    elif bump == "patch":
        patch += 1
    else:
        raise ReleaseError(f"unsupported version bump: {bump!r}")
    return f"{major}.{minor}.{patch}"


def bumped_version(version: str, bump: str) -> str:
    parsed = parse_version(version)
    if parsed.stage is not None:
        raise ReleaseError("finalize the current prerelease before choosing a new version bump")
    return _bumped_release_line(parsed, bump)


def next_version(
    previous: str,
    bump: str | None,
    prerelease: str | None,
    final: bool,
) -> str:
    parsed = parse_version(previous)
    if final:
        if bump or prerelease:
            raise ReleaseError("--final cannot be combined with --bump or --prerelease")
        if parsed.stage is None:
            raise ReleaseError("the current version is already final")
        return parsed.base
    if prerelease:
        stage = STAGE_NAMES[prerelease]
        if bump:
            return f"{_bumped_release_line(parsed, bump)}{stage}1"
        if parsed.stage is None:
            raise ReleaseError("the first prerelease for a release line requires --bump")
        if STAGE_ORDER[stage] < STAGE_ORDER[parsed.stage]:
            raise ReleaseError("a prerelease stage cannot move backwards")
        serial = (parsed.serial or 0) + 1 if stage == parsed.stage else 1
        return f"{parsed.base}{stage}{serial}"
    if not bump:
        raise ReleaseError("choose --bump, --prerelease, or --final")
    return bumped_version(previous, bump)


def _replace_once(path: Path, pattern: str, replacement: str, *, expected: int = 1) -> None:
    updated, count = re.subn(
        pattern,
        replacement,
        path.read_text(),
        count=expected,
        flags=re.MULTILINE | re.DOTALL,
    )
    if count != expected:
        raise ReleaseError(f"expected exactly {expected} release version occurrence(s) in {path}")
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


def _unreleased_section(changelog: str) -> tuple[re.Match[str], int]:
    heading = re.search(r"(?m)^## Unreleased[ \t]*$", changelog)
    if not heading:
        raise ReleaseError("CHANGELOG.md lacks an Unreleased section")
    next_heading = re.search(r"(?m)^## ", changelog[heading.end() :])
    section_end = heading.end() + (next_heading.start() if next_heading else len(changelog))
    if not changelog[heading.end() : section_end].strip():
        raise ReleaseError("CHANGELOG.md Unreleased section is empty")
    return heading, section_end


def prepare_release(
    root: Path,
    bump: str | None = None,
    released_on: date | None = None,
    *,
    prerelease: str | None = None,
    final: bool = False,
) -> str:
    previous = current_version(root)
    version = next_version(previous, bump, prerelease, final)
    released_on = released_on or date.today()

    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text()
    heading, _ = _unreleased_section(changelog)
    if parse_version(version).stage is None:
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
    previous_npm = npm_version(previous)
    version_npm = npm_version(version)
    _replace_once(
        root / "packages/typescript/README.md",
        rf"(environment-harness-client-){re.escape(previous_npm)}(\.tgz)",
        rf"\g<1>{version_npm}\g<2>",
        expected=2,
    )
    _replace_once(
        root / "docs/STATUS.md",
        rf"(^EnvironmentHarness ){re.escape(previous)}( focuses)",
        rf"\g<1>{version}\g<2>",
    )
    _update_json_versions(root / "packages/typescript/package.json", version_npm)
    _update_json_versions(root / "packages/typescript/package-lock.json", version_npm, lockfile=True)

    if current_version(root) != version:
        raise ReleaseError("release preparation left inconsistent versions")
    return version


def select_release_tag(version: str, tags: list[str], changelog: str) -> str | None:
    current = parse_version(version)
    if current.stage is None:
        heading = re.compile(rf"(?m)^## (?:\[{re.escape(version)}\]|{re.escape(version)})(?:\s|$)")
        if not heading.search(changelog):
            raise ReleaseError(f"CHANGELOG.md lacks a {version} release section")
    else:
        _unreleased_section(changelog)

    released: dict[tuple[int, int, int, int, int], str] = {}
    for tag in tags:
        if not tag.startswith("v") or not PEP440_RELEASE.fullmatch(tag[1:]):
            continue
        released[parse_version(tag[1:]).key] = tag
    if current.key in released:
        return None
    if any(item > current.key for item in released):
        newest = released[max(released)]
        raise ReleaseError(f"metadata version {version} is older than existing release {newest}")
    return f"v{version}"


def _git_project_version(root: Path, revision: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{revision}:pyproject.toml"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseError(f"release commit {revision!r} lacks readable project metadata")
    try:
        version = tomllib.loads(result.stdout).get("project", {}).get("version")
        if not isinstance(version, str):
            raise ReleaseError(f"release commit {revision!r} lacks a project version")
        parse_version(version)
    except tomllib.TOMLDecodeError as error:
        raise ReleaseError(f"release commit {revision!r} has malformed project metadata") from error
    return version


def validate_release_commit(release_tag: str, revision: str = "HEAD", root: Path = ROOT) -> None:
    version = current_version(root)
    commit_version = _git_project_version(root, f"{revision}^{{commit}}")
    parent_version = _git_project_version(root, f"{revision}^{{commit}}^1")
    if version != commit_version or release_tag != f"v{commit_version}":
        raise ReleaseError(
            f"release tag {release_tag!r}, working metadata {version}, and commit metadata "
            f"{commit_version} do not match"
        )
    if parse_version(parent_version).key >= parse_version(commit_version).key:
        raise ReleaseError(
            "release commit must introduce a version newer than its first parent: "
            f"{parent_version} -> {commit_version}"
        )


def detect_release_tag(root: Path = ROOT) -> str | None:
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    tag = select_release_tag(current_version(root), tags, (root / "CHANGELOG.md").read_text())
    if tag:
        validate_release_commit(tag, root=root)
    return tag


def validate_release_tag(release_tag: str, root: Path = ROOT, *, release_commit: str | None = None) -> None:
    version = current_version(root)
    if release_tag != f"v{version}":
        raise ReleaseError(f"release tag {release_tag!r} does not match metadata version v{version}")
    select_release_tag(version, [], (root / "CHANGELOG.md").read_text())
    if release_commit:
        validate_release_commit(release_tag, release_commit, root)


def resolve_release_pr(
    pull_request: dict[str, object], workflow_sha: str, root: Path = ROOT
) -> dict[str, str]:
    number = pull_request.get("number")
    base = pull_request.get("base")
    merge_commit = pull_request.get("merge_commit_sha")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if (
        not isinstance(number, int)
        or number < 1
        or pull_request.get("state") != "closed"
        or not isinstance(pull_request.get("merged_at"), str)
        or not isinstance(base, dict)
        or base.get("ref") != "main"
    ):
        raise ReleaseError("release PR must be merged into main")
    if not FULL_SHA.fullmatch(workflow_sha) or merge_commit != workflow_sha or head != workflow_sha:
        raise ReleaseError("workflow must run from the exact merged release PR commit")
    tag = detect_release_tag(root)
    if tag is None:
        tag = f"v{current_version(root)}"
        existing = subprocess.run(
            ["git", "rev-parse", f"{tag}^{{commit}}"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if existing.returncode != 0 or existing.stdout.strip() != workflow_sha:
            raise ReleaseError("release version already has a tag at a different commit")
        validate_release_commit(tag, root=root)
    return {
        "pr": str(number),
        "revision": workflow_sha,
        "tag": tag,
        "version": current_version(root),
    }


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
    prepare.add_argument("--bump", choices=("patch", "minor", "major"))
    prepare.add_argument("--prerelease", choices=("alpha", "beta", "rc"))
    prepare.add_argument("--final", action="store_true")
    prepare.add_argument("--github-output", type=Path)
    detect = subparsers.add_parser("detect")
    detect.add_argument("--github-output", type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--release-tag", required=True)
    validate.add_argument("--release-commit")
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--pull-request-file", type=Path, required=True)
    resolve.add_argument("--workflow-sha", required=True)
    resolve.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    if args.command == "prepare":
        pending = detect_release_tag(ROOT)
        if pending:
            raise ReleaseError(f"current metadata has not been released as {pending}")
        version = prepare_release(
            ROOT,
            args.bump,
            prerelease=args.prerelease,
            final=args.final,
        )
        _write_outputs(args.github_output, {"version": version, "tag": f"v{version}"})
    elif args.command == "detect":
        tag = detect_release_tag(ROOT)
        _write_outputs(
            args.github_output,
            {"create": "true" if tag else "false", "tag": tag or ""},
        )
    elif args.command == "validate":
        validate_release_tag(args.release_tag, release_commit=args.release_commit)
    else:
        pull_request = json.loads(args.pull_request_file.read_text())
        if not isinstance(pull_request, dict):
            raise ReleaseError("pull request response must be a JSON object")
        _write_outputs(
            args.github_output,
            resolve_release_pr(pull_request, args.workflow_sha),
        )


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as error:
        raise SystemExit(f"release preparation failed: {error}") from error
