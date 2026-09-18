"""Fail-closed repository, dependency, workflow, and artifact policy checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MINIMUM_AGE = timedelta(hours=168)
REGISTRY = "https://registry.npmjs.org/"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
ACTION = re.compile(r"\buses:\s*([^\s#]+)(?:\s*#.*)?$")
CONTAINER_IMAGE = re.compile(r"[A-Za-z0-9._/-]+:[^\s@]+@sha256:[0-9a-f]{64}")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
APP_CREDENTIAL_NAME = "_".join(("RELEASE", "APP", "PRIVATE", "KEY"))
SECRET_PATTERNS = {
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(r"\b(?:gh[opurs]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
SAFE_CHILD_ENV = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


class PolicyError(RuntimeError):
    """A repository policy could not be established."""


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    version: str


def _run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
        env={
            **{key: value for key, value in os.environ.items() if key in SAFE_CHILD_ENV},
            "PYTHONPATH": str(ROOT / "src"),
        },
    )
    return result.stdout if capture else ""


def _git_file(reference: str | None, path: str) -> bytes | None:
    if not reference:
        return None
    result = subprocess.run(
        ["git", "show", f"{reference}:{path}"], cwd=ROOT, capture_output=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def _default_base() -> str | None:
    explicit = os.environ.get("DEPENDENCY_BASE_REF")
    if explicit:
        return explicit
    github_base = os.environ.get("GITHUB_BASE_REF")
    candidates = ([f"origin/{github_base}"] if github_base else []) + ["origin/main"]
    for candidate in candidates:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate], cwd=ROOT, capture_output=True, check=False
        )
        if result.returncode == 0:
            return candidate
    return None


def _json_request(url: str) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "environment-harness-policy-check"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.HTTPError) as error:
        raise PolicyError(f"publication metadata unavailable for {url}: {error}") from error


def _timestamp(value: object, description: str) -> datetime:
    if not isinstance(value, str):
        raise PolicyError(f"missing or malformed {description}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PolicyError(f"missing or malformed {description}: {value!r}") from error
    if parsed.tzinfo is None:
        raise PolicyError(f"timestamp has no timezone for {description}")
    return parsed.astimezone(UTC)


def _tracked_files() -> list[Path]:
    output = _run("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", capture=True)
    return [ROOT / item for item in output.split("\0") if item]


def check_secrets(now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    document = json.loads((ROOT / ".github/secret-exceptions.json").read_text())
    if set(document) != {"exceptions"} or not isinstance(document["exceptions"], list):
        raise PolicyError("secret exception file must contain only an exceptions list")
    required = {
        "path",
        "line",
        "detector",
        "fingerprint",
        "owner",
        "reason",
        "approved_by",
        "created_at",
        "expires_at",
    }
    exceptions = {}
    for item in document["exceptions"]:
        if not isinstance(item, dict) or set(item) != required:
            raise PolicyError("secret exception has missing or unexpected fields")
        if any(not item[field] for field in ("owner", "reason", "approved_by")):
            raise PolicyError("secret exception lacks an owner, reason, or security approval")
        created = _timestamp(item["created_at"], "secret exception creation")
        expires = _timestamp(item["expires_at"], "secret exception expiry")
        if created > now or expires <= now or expires > created + timedelta(days=90):
            raise PolicyError("secret exception is future-dated, expired, or longer than 90 days")
        key = (item["path"], item["line"], item["detector"], item["fingerprint"])
        if key in exceptions:
            raise PolicyError("duplicate secret exception")
        exceptions[key] = item
    findings: list[str] = []
    allowed_locations: set[tuple[str, int]] = set()
    for path in _tracked_files():
        if not path.is_file() or path.name == ".secrets.baseline":
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for name, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(line):
                    relative = path.relative_to(ROOT).as_posix()
                    fingerprint = hashlib.sha256(match.group(0).encode()).hexdigest()
                    key = (relative, line_number, name, fingerprint)
                    if key in exceptions:
                        exceptions.pop(key)
                        allowed_locations.add((relative, line_number))
                    else:
                        findings.append(f"{relative}:{line_number}: {name}")
    if (ROOT / ".git").exists() and shutil.which("detect-secrets"):
        report = json.loads(_run("detect-secrets", "scan", capture=True))
        for filename, detected in report.get("results", {}).items():
            for item in detected:
                line_number = item.get("line_number")
                if (filename, line_number) in allowed_locations:
                    continue
                key = (filename, line_number, item.get("type"), item.get("hashed_secret"))
                if key in exceptions:
                    exceptions.pop(key)
                else:
                    findings.append(f"{filename}:{line_number}: {item.get('type')}")
    if findings:
        raise PolicyError("potential secrets found:\n" + "\n".join(findings))
    if exceptions:
        raise PolicyError("secret exception no longer matches an exact finding")


def check_licenses() -> None:
    license_text = (ROOT / "LICENSE").read_text()
    if not license_text.startswith("MIT License\n") or "Permission is hereby granted" not in license_text:
        raise PolicyError("root MIT license is missing or modified")
    if (ROOT / "packages/typescript/LICENSE").read_text() != license_text:
        raise PolicyError("TypeScript artifact license differs from the root MIT license")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    if project["project"].get("license") != "MIT" or project["project"].get("license-files") != ["LICENSE"]:
        raise PolicyError("Python artifact does not declare and include the MIT license")
    package = json.loads((ROOT / "packages/typescript/package.json").read_text())
    if package.get("license") != "MIT" or "LICENSE" not in package.get("files", []):
        raise PolicyError("TypeScript artifact does not declare and include the MIT license")


def check_repository_metadata() -> None:
    stale = "github.com/pollice-verso/environment-harness"
    for path in _tracked_files():
        if path.is_file() and path.suffix in {".md", ".toml", ".json"}:
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if stale in text:
                raise PolicyError(f"stale repository URL in {path.relative_to(ROOT)}")
    required = [
        "CODE_OF_CONDUCT.md",
        "MAINTAINERS.md",
        "SUPPORT.md",
        "SECURITY.md",
        ".github/CODEOWNERS",
        ".github/pull_request_template.md",
    ]
    missing = [path for path in required if not (ROOT / path).is_file()]
    if missing:
        raise PolicyError("missing governance files: " + ", ".join(missing))


def check_version_metadata(release_tag: str | None = None) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package = json.loads((ROOT / "packages/typescript/package.json").read_text())
    package_lock = json.loads((ROOT / "packages/typescript/package-lock.json").read_text())
    package_init = (ROOT / "src/environment_harness/__init__.py").read_text()
    exported = re.search(r'^__version__\s*=\s*"([^"]+)"$', package_init, re.MULTILINE)
    versions = {
        "Python project": project.get("project", {}).get("version"),
        "Python package": exported.group(1) if exported else None,
        "npm package": package.get("version"),
        "npm lockfile": package_lock.get("version"),
        "npm lockfile root": package_lock.get("packages", {}).get("", {}).get("version"),
    }
    if len(set(versions.values())) != 1:
        details = ", ".join(f"{name}={version!r}" for name, version in versions.items())
        raise PolicyError(f"release versions differ: {details}")
    version = next(iter(versions.values()))
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise PolicyError(f"release version is not strict three-component SemVer: {version!r}")
    if release_tag is not None and release_tag != f"v{version}":
        raise PolicyError(f"release tag {release_tag!r} does not match metadata version v{version}")


def check_release_commit(release_tag: str) -> None:
    parent = _git_file(f"{release_tag}^{{commit}}^1", "pyproject.toml")
    if parent is None:
        raise PolicyError("release tag lacks a readable first-parent project version")
    parent_version = tomllib.loads(parent.decode()).get("project", {}).get("version")
    current_version = release_tag.removeprefix("v")
    if not isinstance(parent_version, str) or not SEMVER.fullmatch(parent_version):
        raise PolicyError("release commit parent has a malformed project version")
    previous = tuple(int(part) for part in parent_version.split("."))
    current = tuple(int(part) for part in current_version.split("."))
    if previous >= current:
        raise PolicyError(
            f"release commit must introduce a version newer than its first parent: "
            f"{parent_version} -> {current_version}"
        )


def check_release_workflow_binding() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text()
    release_required = {
        '"v[0-9]+.[0-9]+.[0-9]+"': "strict SemVer tag trigger",
        "RELEASE_TAG: ${{ github.ref_name }}": "event tag binding",
        'test "$GITHUB_SHA" = "$release_commit"': "attested workflow commit",
        'git merge-base --is-ancestor "$release_commit" origin/main': "main ancestry",
        '--release-tag "$RELEASE_TAG"': "tag-to-package version binding",
        "uv build --no-build-isolation": "frozen build environment",
        'gh attestation verify "$artifact"': "per-artifact provenance verification",
        'cmp "$artifact" "$released/$(basename "$artifact")"': "idempotent artifact comparison",
    }
    missing = [description for snippet, description in release_required.items() if snippet not in release]
    if missing:
        raise PolicyError("release workflow lacks " + ", ".join(missing))
    artifact_downloads = release.count("artifact-ids: ${{ needs.build.outputs.artifact-id }}")
    if artifact_downloads == 0 or release.count("merge-multiple: true") < artifact_downloads:
        raise PolicyError("release workflow lacks flat artifact downloads")
    forbidden = {
        "actions/create-github-app-token@": "GitHub App private-key authentication",
        "private-key:": "private-key input",
        APP_CREDENTIAL_NAME: "long-lived release credential",
    }
    workflow_text = "\n".join(
        line.split("#", 1)[0]
        for path in sorted((ROOT / ".github/workflows").glob("*.y*ml"))
        for line in path.read_text().splitlines()
    )
    present = [description for snippet, description in forbidden.items() if snippet in workflow_text]
    if present:
        raise PolicyError("workflows use prohibited " + ", ".join(present))


def check_dependency_configuration() -> None:
    uv_config = tomllib.loads((ROOT / "uv.toml").read_text())
    if uv_config.get("exclude-newer") != "1 week":
        raise PolicyError("uv.toml exclude-newer must be exactly '1 week'")
    npmrc = dict(
        line.split("=", 1)
        for line in (ROOT / ".npmrc").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    if npmrc.get("min-release-age") != "7" or npmrc.get("ignore-scripts") != "true":
        raise PolicyError(".npmrc must enforce min-release-age=7 and ignore-scripts=true")
    package = json.loads((ROOT / "packages/typescript/package.json").read_text())
    if not str(package.get("engines", {}).get("npm", "")).startswith(">=11.17"):
        raise PolicyError("package metadata must require npm >=11.17")
    if not str(package.get("packageManager", "")).startswith("npm@11.17"):
        raise PolicyError("packageManager must pin the supported npm 11.17 release")
    dependabot = (ROOT / ".github/dependabot.yml").read_text()
    for ecosystem in ("pip", "npm", "github-actions"):
        block = dependabot.split(f"package-ecosystem: {ecosystem}", 1)
        if len(block) != 2:
            raise PolicyError(f"Dependabot {ecosystem} updates are not configured")
        ecosystem_config = block[1].split("package-ecosystem:", 1)[0]
        if "default-days: 7" not in ecosystem_config:
            raise PolicyError(f"Dependabot {ecosystem} updates lack a seven-day cooldown")
        if "interval: monthly" not in ecosystem_config:
            raise PolicyError(f"Dependabot {ecosystem} routine updates must run monthly")
        if "open-pull-requests-limit: 1" not in ecosystem_config:
            raise PolicyError(f"Dependabot {ecosystem} must limit routine update pull requests")
        if not all(
            setting in ecosystem_config
            for setting in (
                "routine-version-updates:",
                "applies-to: version-updates",
                '          - "*"',
            )
        ):
            raise PolicyError(f"Dependabot {ecosystem} routine updates must be grouped")


def _uv_dependencies(content: bytes) -> set[Dependency]:
    parsed = tomllib.loads(content.decode())
    dependencies = set()
    for package in parsed.get("package", []):
        source = package.get("source", {})
        if "registry" in source:
            if source["registry"] != "https://pypi.org/simple":
                raise PolicyError(f"unapproved Python registry for {package['name']}")
            artifacts = ([package["sdist"]] if isinstance(package.get("sdist"), dict) else []) + list(
                package.get("wheels", [])
            )
            if not artifacts or any(
                not isinstance(artifact.get("hash"), str) or not artifact["hash"].startswith("sha256:")
                for artifact in artifacts
            ):
                raise PolicyError(f"Python dependency lacks locked SHA-256 artifacts: {package['name']}")
            dependencies.add(Dependency("pypi", package["name"], package["version"]))
        elif any(key in source for key in ("git", "url")):
            raise PolicyError(f"Git or URL Python dependency is prohibited: {package['name']}")
    return dependencies


def _npm_dependencies(content: bytes) -> set[Dependency]:
    parsed = json.loads(content)
    dependencies = set()
    for path, package in parsed.get("packages", {}).items():
        if not path or "node_modules/" not in path:
            continue
        resolved = package.get("resolved")
        if not isinstance(resolved, str) or not resolved.startswith(REGISTRY):
            raise PolicyError(f"unapproved npm dependency source: {path}")
        if not isinstance(package.get("integrity"), str) or not package["integrity"].startswith("sha512-"):
            raise PolicyError(f"npm dependency lacks locked SHA-512 integrity: {path}")
        name = urllib.parse.unquote(path.rsplit("node_modules/", 1)[1])
        dependencies.add(Dependency("npm", name, package["version"]))
    return dependencies


def _publication_time(dependency: Dependency) -> datetime:
    if dependency.ecosystem == "pypi":
        name = urllib.parse.quote(dependency.name, safe="")
        version = urllib.parse.quote(dependency.version, safe="")
        metadata = _json_request(f"https://pypi.org/pypi/{name}/{version}/json")
        uploads = [
            _timestamp(item.get("upload_time_iso_8601"), f"{dependency.name} {dependency.version}")
            for item in metadata.get("urls", [])
        ]
        if not uploads:
            raise PolicyError(f"missing upload timestamps for {dependency.name} {dependency.version}")
        return max(uploads)
    name = urllib.parse.quote(dependency.name, safe="")
    metadata = _json_request(f"{REGISTRY}{name}")
    return _timestamp(
        metadata.get("time", {}).get(dependency.version),
        f"{dependency.name} {dependency.version}",
    )


def _load_exceptions(now: datetime) -> dict[Dependency, dict[str, Any]]:
    document = json.loads((ROOT / ".github/dependency-exceptions.json").read_text())
    if set(document) != {"exceptions"} or not isinstance(document["exceptions"], list):
        raise PolicyError("dependency exception file must contain only an exceptions list")
    required = {
        "ecosystem",
        "package",
        "version",
        "issue",
        "advisory",
        "operational_impact",
        "rationale",
        "owner",
        "approved_by",
        "published_at",
        "reviewed_at",
        "expires_at",
        "provenance_checked",
        "checksums_verified",
        "signatures_or_attestations_checked",
        "malware_scan",
        "vulnerabilities_reviewed",
        "maintainer_changes_reviewed",
        "install_scripts_reviewed",
    }
    checks = required - {
        "ecosystem",
        "package",
        "version",
        "issue",
        "advisory",
        "operational_impact",
        "rationale",
        "owner",
        "approved_by",
        "published_at",
        "reviewed_at",
        "expires_at",
    }
    result: dict[Dependency, dict[str, Any]] = {}
    for item in document["exceptions"]:
        if not isinstance(item, dict) or set(item) != required:
            raise PolicyError("dependency exception has missing or unexpected fields")
        if any(
            not item[field]
            for field in ("issue", "advisory", "operational_impact", "rationale", "owner", "approved_by")
        ):
            raise PolicyError("dependency exception has an empty issue, risk, owner, or approval")
        if not str(item["issue"]).startswith("https://github.com/kimpton-ai/environment-harness/issues/"):
            raise PolicyError("dependency exception must reference a tracked public repository issue")
        if item["approved_by"] != "@kimpton-ai/security":
            raise PolicyError("dependency exception requires approval from @kimpton-ai/security")
        if any(
            not isinstance(item[field], str)
            or len(item[field]) < 24
            or not item[field].startswith("https://")
            for field in checks
        ):
            raise PolicyError("dependency exception security review needs linked evidence for every check")
        published = _timestamp(item["published_at"], "exception publication")
        reviewed = _timestamp(item["reviewed_at"], "exception security review")
        expires = _timestamp(item["expires_at"], "exception expiry")
        if reviewed < published or reviewed > now:
            raise PolicyError("dependency exception security review is stale or future-dated")
        if expires <= now or expires > published + MINIMUM_AGE:
            raise PolicyError("dependency exception is expired or extends past the cooldown")
        dependency = Dependency(item["ecosystem"], item["package"], item["version"])
        if dependency in result:
            raise PolicyError(f"duplicate exception for {dependency.name} {dependency.version}")
        result[dependency] = item
    return result


def check_dependency_ages(base_ref: str | None, now: datetime) -> None:
    exceptions = _load_exceptions(now)
    for path, parser in (
        ("uv.lock", _uv_dependencies),
        ("packages/typescript/package-lock.json", _npm_dependencies),
    ):
        proposed = parser((ROOT / path).read_bytes())
        base_content = _git_file(base_ref, path)
        base = parser(base_content) if base_content else set()
        for dependency in sorted(proposed - base, key=lambda item: (item.ecosystem, item.name, item.version)):
            published = _publication_time(dependency)
            if published > now:
                raise PolicyError(f"future publication timestamp for {dependency.name} {dependency.version}")
            if now - published < MINIMUM_AGE and dependency not in exceptions:
                age = now - published
                raise PolicyError(
                    f"{dependency.ecosystem} dependency {dependency.name} {dependency.version} is only {age} old"
                )
            if dependency in exceptions:
                recorded = _timestamp(exceptions[dependency]["published_at"], "exception publication")
                if recorded != published:
                    raise PolicyError(f"publication mismatch in exception for {dependency.name}")


def _workflow_actions(text: str) -> list[tuple[str, str]]:
    actions = []
    for line in text.splitlines():
        match = ACTION.search(line)
        if not match or match.group(1).startswith("./"):
            continue
        target = match.group(1)
        if "@" not in target:
            raise PolicyError(f"malformed Action reference: {target}")
        repository, revision = target.rsplit("@", 1)
        if not FULL_SHA.fullmatch(revision):
            raise PolicyError(f"Action is not pinned to a full commit: {target}")
        actions.append((repository, revision))
    return actions


def _verify_action_release(key: str, pin: dict[str, str], now: datetime) -> None:
    action, revision = key.rsplit("@", 1)
    repository = "/".join(action.split("/")[:2])
    release = _json_request(
        f"https://api.github.com/repos/{repository}/releases/tags/{urllib.parse.quote(pin['release'], safe='')}"
    )
    published = _timestamp(release.get("published_at"), f"GitHub release {repository} {pin['release']}")
    recorded = _timestamp(pin["published_at"], f"Action release {key}")
    if published != recorded or published > now or now - published < MINIMUM_AGE:
        raise PolicyError(f"Action release metadata does not establish a 168-hour cooldown: {key}")
    reference = _json_request(
        f"https://api.github.com/repos/{repository}/git/ref/tags/{urllib.parse.quote(pin['release'], safe='')}"
    )
    target = reference.get("object", {})
    if target.get("type") == "tag":
        tag = _json_request(f"https://api.github.com/repos/{repository}/git/tags/{target.get('sha', '')}")
        target = tag.get("object", {})
    if target.get("type") != "commit" or target.get("sha") != revision:
        raise PolicyError(f"Action SHA does not correspond to reviewed release {pin['release']}: {key}")


def check_workflows(now: datetime, full: bool) -> None:
    pin_file = ROOT / ".github/action-pins.json"
    pins = json.loads(pin_file.read_text()) if pin_file.exists() else {}
    used: set[str] = set()
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        text = path.read_text()
        if re.search(r"(?m)^\s*pull_request_target\s*:", text):
            raise PolicyError(f"pull_request_target is prohibited in {path.name}")
        if re.search(r"(?m)^\s*workflow_run\s*:", text):
            raise PolicyError(f"workflow_run is prohibited in {path.name}")
        if re.search(r"(?m)^\s*pull_request\s*:", text):
            if re.search(r"(?m)^\s*id-token:\s*write\s*$", text):
                raise PolicyError(f"pull request workflow {path.name} may not mint OIDC tokens")
            if "secrets." in text:
                raise PolicyError(f"pull request workflow {path.name} may not access repository secrets")
        if not re.search(r"(?m)^permissions:\s*\{\}\s*$", text):
            raise PolicyError(f"{path.name} must default to permissions: {{}}")
        if "actions/checkout" in text and "permissions:\n      contents: read" not in text:
            raise PolicyError(f"checkout jobs in {path.name} must grant only contents: read")
        if "actions/checkout" in text and "persist-credentials: false" not in text:
            raise PolicyError(f"{path.name} checkout must disable persisted credentials")
        for line in text.splitlines():
            service_image = re.search(r"\bimage:\s*(\S+)", line)
            registry_images = re.findall(r"\b(?:docker\.io|ghcr\.io|quay\.io)/\S+", line)
            for image in ([service_image.group(1)] if service_image else []) + registry_images:
                if not CONTAINER_IMAGE.fullmatch(image):
                    raise PolicyError(
                        f"container image is not pinned by tag and digest in {path.name}: {image}"
                    )
        for repository, revision in _workflow_actions(text):
            key = f"{repository}@{revision}"
            used.add(key)
            pin = pins.get(key)
            if not isinstance(pin, dict) or set(pin) != {"release", "published_at"}:
                raise PolicyError(f"Action pin lacks reviewed release metadata: {key}")
            published = _timestamp(pin["published_at"], f"Action release {key}")
            if published > now or now - published < MINIMUM_AGE:
                raise PolicyError(f"Action release is younger than 168 hours: {key}")
            if full and not pin["release"].startswith("v"):
                raise PolicyError(f"Action pin does not identify a versioned release: {key}")
    unused = set(pins) - used
    if unused:
        raise PolicyError("unused Action pin metadata: " + ", ".join(sorted(unused)))
    if full:
        for key in sorted(used):
            _verify_action_release(key, pins[key], now)


def check_generated() -> None:
    _run(sys.executable, "scripts/build_contracts.py", "--check")
    _run(sys.executable, "scripts/build_viewer.py", "--check")


def check_distributions() -> None:
    _run(sys.executable, "scripts/check_distribution.py")


def main() -> None:
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--full", action="store_true", help="query registries and validate changed dependency age"
    )
    parser.add_argument("--base-ref", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--release-tag", default=None, help="require this tag to match package metadata")
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    ROOT = args.root.resolve()
    now = _timestamp(args.now, "current time") if args.now else datetime.now(UTC)
    checks = [
        ("repository metadata", check_repository_metadata),
        ("version metadata", lambda: check_version_metadata(args.release_tag)),
        ("licenses", check_licenses),
        ("secrets", lambda: check_secrets(now)),
        ("release binding", check_release_workflow_binding),
        ("dependency configuration", check_dependency_configuration),
        ("workflow policy", lambda: check_workflows(now, args.full)),
        ("generated files", check_generated),
        ("distributions", check_distributions),
    ]
    if args.release_tag:
        checks.insert(2, ("release commit", lambda: check_release_commit(args.release_tag)))
    if args.full:
        checks.insert(
            4, ("dependency age", lambda: check_dependency_ages(args.base_ref or _default_base(), now))
        )
    for name, check in checks:
        check()
        print(f"{name}: passed")


if __name__ == "__main__":
    try:
        main()
    except (PolicyError, KeyError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise SystemExit(f"repository policy failed: {error}") from error
