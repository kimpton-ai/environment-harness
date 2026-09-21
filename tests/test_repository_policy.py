import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_distribution = load_script("check_distribution")
check_repository = load_script("check_repository")
check_regression_tests = load_script("check_regression_tests")
LEGACY_APP_CREDENTIAL = "_".join(("RELEASE", "APP", "PRIVATE", "KEY"))


def configure_release_workflows(tmp_path):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / ".github/workflows"
    shutil.copyfile(source / "release.yml", workflows / "release.yml")
    return workflows


def configure_versions(tmp_path, python_version="0.2.0", npm_version="0.2.0", lock_version="0.2.0"):
    package = tmp_path / "packages/typescript"
    python_package = tmp_path / "src/environment_harness"
    package.mkdir(parents=True)
    python_package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "fixture"\nversion = "{python_version}"\n')
    (package / "package.json").write_text(json.dumps({"version": npm_version}))
    (package / "package-lock.json").write_text(
        json.dumps({"version": lock_version, "packages": {"": {"version": lock_version}}})
    )
    (python_package / "__init__.py").write_text(f'__version__ = "{python_version}"\n')


def test_release_versions_and_tag_are_consistent(tmp_path, monkeypatch):
    configure_versions(tmp_path)
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    check_repository.check_version_metadata("v0.2.0")


def test_ci_workflow_and_classifier_require_security_ownership(tmp_path, monkeypatch):
    github = tmp_path / ".github"
    github.mkdir()
    (github / "CODEOWNERS").write_text(
        "/.github/workflows/ci.yml @kimpton-ai/security\n"
        "/scripts/classify_ci_changes.py @kimpton-ai/security\n"
    )
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    check_repository.check_ci_ownership()


def test_ci_security_ownership_fails_closed(tmp_path, monkeypatch):
    github = tmp_path / ".github"
    github.mkdir()
    (github / "CODEOWNERS").write_text(
        "/.github/workflows/ci.yml @kimpton-ai/maintainers\n"
        "/scripts/classify_ci_changes.py @kimpton-ai/maintainers\n"
    )
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="CI security ownership"):
        check_repository.check_ci_ownership()


def test_pep440_prerelease_maps_to_npm_semver(tmp_path, monkeypatch):
    configure_versions(tmp_path, "0.3.0rc2", "0.3.0-rc.2", "0.3.0-rc.2")
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    check_repository.check_version_metadata("v0.3.0rc2")


@pytest.mark.parametrize(
    ("python_version", "npm_version", "lock_version", "tag"),
    [
        ("0.2.0", "0.2.1", "0.2.0", None),
        ("0.2", "0.2", "0.2", None),
        ("0.2.0", "0.2.0", "0.2.0", "v0.2.1"),
    ],
)
def test_release_version_mismatch_fails_closed(
    tmp_path, monkeypatch, python_version, npm_version, lock_version, tag
):
    configure_versions(tmp_path, python_version, npm_version, lock_version)
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError):
        check_repository.check_version_metadata(tag)


@pytest.mark.parametrize("parent_version", ["0.2.0", "0.3.0", "invalid"])
def test_release_commit_must_introduce_a_newer_version(monkeypatch, parent_version):
    parent = f'[project]\nversion = "{parent_version}"\n'.encode()
    monkeypatch.setattr(check_repository, "_git_file", lambda _reference, _path: parent)

    with pytest.raises(check_repository.PolicyError, match="version"):
        check_repository.check_release_commit("v0.2.0")


def test_release_commit_accepts_version_introducing_parent(monkeypatch):
    parent = b'[project]\nversion = "0.1.0"\n'
    monkeypatch.setattr(check_repository, "_git_file", lambda _reference, _path: parent)

    check_repository.check_release_commit("v0.2.0")


def test_release_workflow_requires_commit_binding(tmp_path, monkeypatch):
    workflows = configure_release_workflows(tmp_path)
    release = workflows / "release.yml"
    release.write_text(release.read_text().replace('test "$GITHUB_SHA" = "$release_commit"', "true"))
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="attested workflow commit"):
        check_repository.check_release_workflow_binding()


def test_release_workflow_flattens_artifact_downloads(tmp_path, monkeypatch):
    workflows = configure_release_workflows(tmp_path)
    release = workflows / "release.yml"
    release.write_text(release.read_text().replace("          merge-multiple: true\n", "", 1))
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="flat artifact downloads"):
        check_repository.check_release_workflow_binding()


def test_release_workflow_requires_artifact_downloads(tmp_path, monkeypatch):
    workflows = configure_release_workflows(tmp_path)
    release = workflows / "release.yml"
    release.write_text(
        release.read_text().replace(
            "artifact-ids: ${{ needs.build.outputs.artifact-id }}",
            "name: release-artifacts",
        )
    )
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="flat artifact downloads"):
        check_repository.check_release_workflow_binding()


@pytest.mark.parametrize(
    ("configured", "invalid", "message"),
    [
        ("interval: monthly", "interval: weekly", "must run monthly"),
        ("open-pull-requests-limit: 1", "open-pull-requests-limit: 5", "must limit"),
        ("routine-version-updates:", "unscoped-updates:", "must be grouped"),
    ],
)
def test_dependabot_routine_update_policy_fails_closed(tmp_path, monkeypatch, configured, invalid, message):
    root = Path(__file__).resolve().parents[1]
    github = tmp_path / ".github"
    github.mkdir()
    package = tmp_path / "packages/typescript"
    package.mkdir(parents=True)
    shutil.copyfile(root / "uv.toml", tmp_path / "uv.toml")
    shutil.copyfile(root / ".npmrc", tmp_path / ".npmrc")
    shutil.copyfile(root / "packages/typescript/package.json", package / "package.json")
    source = root / ".github/dependabot.yml"
    (github / "dependabot.yml").write_text(source.read_text().replace(configured, invalid, 1))
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match=message):
        check_repository.check_dependency_configuration()


@pytest.mark.parametrize(
    ("required", "description"),
    [
        ('"v[0-9]+.[0-9]+.[0-9]+"', "final PEP 440 tag trigger"),
        ('"v[0-9]+.[0-9]+.[0-9]+rc[0-9]+"', "release-candidate tag trigger"),
        ("RELEASE_TAG: ${{ github.ref_name }}", "event tag binding"),
        ('cache: ""', "disabled setup-node package cache"),
        ("cd dist && sha256sum -- * > SHA256SUMS", "download-friendly checksum paths"),
    ],
)
def test_release_workflow_requires_tag_event_binding(tmp_path, monkeypatch, required, description):
    workflows = configure_release_workflows(tmp_path)
    release = workflows / "release.yml"
    release.write_text(release.read_text().replace(required, "removed"))
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match=description):
        check_repository.check_release_workflow_binding()


@pytest.mark.parametrize(
    "credential",
    ["actions/create-github-app-token@", "private-key:", LEGACY_APP_CREDENTIAL],
)
def test_release_workflows_reject_long_lived_app_credentials(tmp_path, monkeypatch, credential):
    workflows = configure_release_workflows(tmp_path)
    (workflows / "unsafe-release.yml").write_text(f"permissions: {{}}\n{credential}\n")
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="prohibited"):
        check_repository.check_release_workflow_binding()


def test_release_workflow_credential_comments_are_not_policy_violations(tmp_path, monkeypatch):
    workflows = configure_release_workflows(tmp_path)
    (workflows / "comment.yml").write_text("permissions: {}\n# private-key: is prohibited\n")
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    check_repository.check_release_workflow_binding()


def test_release_workflows_exclude_long_lived_app_credentials():
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    workflow_text = "\n".join(path.read_text() for path in workflows.glob("*.y*ml"))

    assert "actions/create-github-app-token@" not in workflow_text
    assert "private-key:" not in workflow_text
    assert LEGACY_APP_CREDENTIAL not in workflow_text


def test_regression_proof_requires_an_actual_test_failure():
    for returncode in (1, 2):
        check_regression_tests.require_regression_test_failure(returncode)
    with pytest.raises(SystemExit, match="also pass"):
        check_regression_tests.require_regression_test_failure(0)
    for returncode in (3, 4, 5):
        with pytest.raises(SystemExit, match=f"exit code {returncode}"):
            check_regression_tests.require_regression_test_failure(returncode)


def configure_release_tree(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in (
        "CHANGELOG.md",
        "README.md",
        "docs/STATUS.md",
        "pyproject.toml",
        "packages/typescript/README.md",
        "uv.lock",
        "packages/typescript/package.json",
        "packages/typescript/package-lock.json",
        "src/environment_harness/__init__.py",
    ):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, destination)
    current_python = re.search(r'(?m)^version = "([^"]+)"$', (tmp_path / "pyproject.toml").read_text()).group(
        1
    )
    current_npm = check_repository._npm_version(current_python)
    for name in (
        "README.md",
        "docs/STATUS.md",
        "pyproject.toml",
        "src/environment_harness/__init__.py",
        "uv.lock",
    ):
        path = tmp_path / name
        path.write_text(path.read_text().replace(current_python, "0.2.2"))
    for name in (
        "packages/typescript/README.md",
        "packages/typescript/package.json",
        "packages/typescript/package-lock.json",
    ):
        path = tmp_path / name
        path.write_text(path.read_text().replace(current_npm, "0.2.2"))
    changelog = (tmp_path / "CHANGELOG.md").read_text()
    (tmp_path / "CHANGELOG.md").write_text(
        changelog.replace("## Unreleased\n", "## Unreleased\n\n- Added a synthetic release note.\n", 1)
    )


def test_prepare_release_updates_all_version_surfaces(tmp_path):
    release_version = load_script("release_version")
    configure_release_tree(tmp_path)

    version = release_version.prepare_release(tmp_path, "minor", date(2026, 9, 18))

    assert version == "0.3.0"
    assert release_version.current_version(tmp_path) == version
    assert 'name = "environment-harness"\nversion = "0.3.0"' in (tmp_path / "uv.lock").read_text()
    assert 'python -m pip install "environment-harness[server]"' in (tmp_path / "README.md").read_text()
    assert "--branch v" not in (tmp_path / "README.md").read_text()
    assert "environment-harness-client-0.3.0.tgz" in (tmp_path / "packages/typescript/README.md").read_text()
    assert (
        "environment-harness-client-0.2.2.tgz" not in (tmp_path / "packages/typescript/README.md").read_text()
    )
    assert "EnvironmentHarness 0.3.0 focuses" in (tmp_path / "docs/STATUS.md").read_text()
    assert (
        "## 0.3.0 - 2026-09-18\n\n- Added a synthetic release note."
        in (tmp_path / "CHANGELOG.md").read_text()
    )


def test_prepare_release_candidate_increment_and_finalization(tmp_path):
    release_version = load_script("release_version")
    configure_release_tree(tmp_path)

    first = release_version.prepare_release(
        tmp_path,
        "patch",
        date(2026, 9, 18),
        prerelease="rc",
    )
    assert first == "0.2.3rc1"
    assert json.loads((tmp_path / "packages/typescript/package.json").read_text())["version"] == (
        "0.2.3-rc.1"
    )
    assert "## 0.2.3rc1" not in (tmp_path / "CHANGELOG.md").read_text()
    assert "- Added a synthetic release note." in (tmp_path / "CHANGELOG.md").read_text()

    second = release_version.prepare_release(
        tmp_path,
        prerelease="rc",
        released_on=date(2026, 9, 19),
    )
    assert second == "0.2.3rc2"
    assert json.loads((tmp_path / "packages/typescript/package.json").read_text())["version"] == (
        "0.2.3-rc.2"
    )

    final = release_version.prepare_release(tmp_path, final=True, released_on=date(2026, 9, 20))
    assert final == "0.2.3"
    assert "## 0.2.3 - 2026-09-20" in (tmp_path / "CHANGELOG.md").read_text()
    assert json.loads((tmp_path / "packages/typescript/package.json").read_text())["version"] == "0.2.3"


def test_contributing_names_supported_uv_version():
    root = Path(__file__).resolve().parents[1]

    assert "uv 0.12.0" in (root / "CONTRIBUTING.md").read_text()


def test_prepare_release_requires_unreleased_notes(tmp_path):
    release_version = load_script("release_version")
    configure_release_tree(tmp_path)
    changelog = (tmp_path / "CHANGELOG.md").read_text()
    (tmp_path / "CHANGELOG.md").write_text(
        re.sub(r"(?s)(## Unreleased\n).*?(?=\n## )", r"\1", changelog, count=1)
    )

    with pytest.raises(release_version.ReleaseError, match="Unreleased section is empty"):
        release_version.prepare_release(tmp_path, "patch", date(2026, 9, 18))


def test_release_tag_is_created_once_and_never_for_a_downgrade():
    release_version = load_script("release_version")
    changelog = "# Changelog\n\n## Unreleased\n\n## 0.2.0 - 2026-09-17\n"

    assert release_version.select_release_tag("0.2.0", ["v0.1.0"], changelog) == "v0.2.0"
    assert release_version.select_release_tag("0.2.0", ["v0.1.0", "v0.2.0"], changelog) is None
    with pytest.raises(release_version.ReleaseError, match="older than existing release"):
        release_version.select_release_tag("0.2.0", ["v0.3.0"], changelog)


def test_release_candidate_tags_follow_pep440_ordering():
    release_version = load_script("release_version")
    changelog = "# Changelog\n\n## Unreleased\n\n- Candidate changes.\n\n## 0.2.0\n"

    assert release_version.select_release_tag("0.3.0rc2", ["v0.3.0rc1"], changelog) == "v0.3.0rc2"
    with pytest.raises(release_version.ReleaseError, match="older than existing release"):
        release_version.select_release_tag("0.3.0rc1", ["v0.3.0rc2"], changelog)


def npm_lock(name="example", version="1.0.0"):
    return json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {},
                f"node_modules/{name}": {
                    "version": version,
                    "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz",
                    "integrity": "sha512-c3ludGhldGljLWZpeHR1cmU=",
                },
            },
        }
    ).encode()


def uv_lock(name=None, version="1.0.0"):
    package = ""
    if name:
        package = (
            f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            'sdist = { url = "https://files.pythonhosted.org/example.tar.gz", '
            'hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", '
            'size = 1, upload-time = "2026-01-01T00:00:00Z" }\n'
        )
    return ('version = 1\nrevision = 1\nrequires-python = ">=3.12"\n' + package).encode()


def exception(dependency, published, expires):
    return {
        "ecosystem": dependency.ecosystem,
        "package": dependency.name,
        "version": dependency.version,
        "issue": "https://github.com/kimpton-ai/environment-harness/issues/123",
        "advisory": "GHSA-synthetic-test",
        "operational_impact": "Synthetic test impact",
        "rationale": "Waiting is riskier in this synthetic test",
        "owner": "@maintainer",
        "approved_by": "@kimpton-ai/security",
        "published_at": published.isoformat(),
        "reviewed_at": (published + timedelta(minutes=5)).isoformat(),
        "expires_at": expires.isoformat(),
        "provenance_checked": "https://github.com/kimpton-ai/environment-harness/issues/123#provenance",
        "checksums_verified": "https://github.com/kimpton-ai/environment-harness/issues/123#checksums",
        "signatures_or_attestations_checked": "https://github.com/kimpton-ai/environment-harness/issues/123#signatures",
        "malware_scan": "https://github.com/kimpton-ai/environment-harness/issues/123#malware",
        "vulnerabilities_reviewed": "https://github.com/kimpton-ai/environment-harness/issues/123#vulnerabilities",
        "maintainer_changes_reviewed": "https://github.com/kimpton-ai/environment-harness/issues/123#maintainers",
        "install_scripts_reviewed": "https://github.com/kimpton-ai/environment-harness/issues/123#install-scripts",
    }


def configure_dependency_tree(tmp_path, monkeypatch, exceptions=None):
    (tmp_path / "packages/typescript").mkdir(parents=True)
    (tmp_path / ".github").mkdir()
    (tmp_path / "uv.lock").write_bytes(uv_lock())
    (tmp_path / "packages/typescript/package-lock.json").write_bytes(npm_lock())
    (tmp_path / ".github/dependency-exceptions.json").write_text(json.dumps({"exceptions": exceptions or []}))
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    monkeypatch.setattr(check_repository, "_git_file", lambda _ref, _path: None)


def test_dependency_published_just_under_seven_days_fails_closed(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    configure_dependency_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(
        check_repository, "_publication_time", lambda _dependency: now - timedelta(days=6, hours=23)
    )

    with pytest.raises(check_repository.PolicyError, match="only"):
        check_repository.check_dependency_ages(None, now)


def test_dependency_published_at_least_168_hours_passes(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    configure_dependency_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(check_repository, "_publication_time", lambda _dependency: now - timedelta(hours=168))

    check_repository.check_dependency_ages(None, now)


def test_future_dependency_timestamp_fails(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    configure_dependency_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(check_repository, "_publication_time", lambda _dependency: now + timedelta(seconds=1))

    with pytest.raises(check_repository.PolicyError, match="future"):
        check_repository.check_dependency_ages(None, now)


def test_exact_reviewed_exception_allows_young_release(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    dependency = check_repository.Dependency("npm", "example", "1.0.0")
    published = now - timedelta(days=6, hours=23)
    configure_dependency_tree(
        tmp_path,
        monkeypatch,
        [exception(dependency, published, now + timedelta(minutes=30))],
    )
    monkeypatch.setattr(check_repository, "_publication_time", lambda _dependency: published)

    check_repository.check_dependency_ages(None, now)


@pytest.mark.parametrize("mutation", ["package", "version", "owner", "approved_by"])
def test_mismatched_or_ownerless_exception_fails(tmp_path, monkeypatch, mutation):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    dependency = check_repository.Dependency("npm", "example", "1.0.0")
    published = now - timedelta(days=6, hours=23)
    item = exception(dependency, published, now + timedelta(minutes=30))
    item[mutation] = "" if mutation in {"owner", "approved_by"} else "mismatch"
    configure_dependency_tree(tmp_path, monkeypatch, [item])
    monkeypatch.setattr(check_repository, "_publication_time", lambda _dependency: published)

    with pytest.raises(check_repository.PolicyError):
        check_repository.check_dependency_ages(None, now)


def test_non_security_approver_and_boolean_evidence_fail(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    dependency = check_repository.Dependency("npm", "example", "1.0.0")
    published = now - timedelta(days=6, hours=23)
    item = exception(dependency, published, now + timedelta(minutes=30))
    item["approved_by"] = "@kimpton-ai/maintainers"
    item["malware_scan"] = True
    configure_dependency_tree(tmp_path, monkeypatch, [item])
    monkeypatch.setattr(check_repository, "_publication_time", lambda _dependency: published)

    with pytest.raises(check_repository.PolicyError):
        check_repository.check_dependency_ages(None, now)


def test_lockfile_dependencies_require_integrity(tmp_path):
    missing_npm_integrity = json.loads(npm_lock())
    missing_npm_integrity["packages"]["node_modules/example"].pop("integrity")
    with pytest.raises(check_repository.PolicyError, match="integrity"):
        check_repository._npm_dependencies(json.dumps(missing_npm_integrity).encode())

    missing_python_hash = uv_lock("example").replace(b"sha256:", b"sha1:")
    with pytest.raises(check_repository.PolicyError, match="SHA-256"):
        check_repository._uv_dependencies(missing_python_hash)


def test_expired_exception_fails(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    dependency = check_repository.Dependency("npm", "example", "1.0.0")
    published = now - timedelta(days=2)
    configure_dependency_tree(
        tmp_path,
        monkeypatch,
        [exception(dependency, published, now - timedelta(seconds=1))],
    )

    with pytest.raises(check_repository.PolicyError, match="expired"):
        check_repository.check_dependency_ages(None, now)


def test_missing_timestamp_fails_closed():
    with pytest.raises(check_repository.PolicyError, match="missing or malformed"):
        check_repository._timestamp(None, "synthetic publication")


def test_malicious_workflow_trigger_is_rejected(tmp_path, monkeypatch):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (tmp_path / ".github/action-pins.json").write_text("{}")
    (workflows / "unsafe.yml").write_text(
        "name: unsafe\non:\n  pull_request_target:\npermissions:\n  contents: read\njobs: {}\n"
    )
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="pull_request_target"):
        check_repository.check_workflows(datetime(2026, 9, 17, tzinfo=UTC), False)


def test_unpinned_action_is_rejected(tmp_path, monkeypatch):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (tmp_path / ".github/action-pins.json").write_text("{}")
    (workflows / "unsafe.yml").write_text(
        "name: unsafe\non: [push]\npermissions: {}\n"
        "jobs:\n  test:\n    permissions:\n      contents: read\n    steps:\n"
        "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n"
    )
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="not pinned"):
        check_repository.check_workflows(datetime(2026, 9, 17, tzinfo=UTC), False)


@pytest.mark.parametrize(
    ("unsafe", "message"),
    [
        ("    permissions:\n      id-token: write\n", "OIDC"),
        ("    env:\n      TOKEN: ${{ secrets.REPOSITORY_TOKEN }}\n", "repository secrets"),
    ],
)
def test_pull_request_workflow_cannot_receive_credentials(tmp_path, monkeypatch, unsafe, message):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (tmp_path / ".github/action-pins.json").write_text("{}")
    (workflows / "unsafe.yml").write_text(
        "name: unsafe\non:\n  pull_request:\npermissions: {}\njobs:\n  test:\n" + unsafe
    )
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match=message):
        check_repository.check_workflows(datetime(2026, 9, 17, tzinfo=UTC), False)


def test_container_image_requires_immutable_digest(tmp_path, monkeypatch):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (tmp_path / ".github/action-pins.json").write_text("{}")
    (workflows / "unsafe.yml").write_text(
        "name: unsafe\non: [push]\npermissions: {}\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: docker run ghcr.io/example/tool:v1 scan\n"
    )
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)

    with pytest.raises(check_repository.PolicyError, match="not pinned by tag and digest"):
        check_repository.check_workflows(datetime(2026, 9, 17, tzinfo=UTC), False)


def test_secret_scanner_rejects_planted_credential(tmp_path, monkeypatch):
    planted = tmp_path / "planted.txt"
    planted.write_text("credential=" + "AKIA" + "A" * 16)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/secret-exceptions.json").write_text('{"exceptions": []}')
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    monkeypatch.setattr(check_repository, "_tracked_files", lambda: [planted])

    with pytest.raises(check_repository.PolicyError, match="AWS access key"):
        check_repository.check_secrets()


def test_exact_unexpired_secret_exception_is_accepted(tmp_path, monkeypatch):
    now = datetime(2026, 9, 17, tzinfo=UTC)
    secret = "AKIA" + "B" * 16  # pragma: allowlist secret
    planted = tmp_path / "planted.txt"
    planted.write_text("credential=" + secret)
    (tmp_path / ".github").mkdir()
    item = {
        "path": "planted.txt",
        "line": 1,
        "detector": "AWS access key",
        "fingerprint": hashlib.sha256(secret.encode()).hexdigest(),
        "owner": "@maintainer",
        "reason": "Synthetic scanner regression fixture",
        "approved_by": "@kimpton-ai/security",
        "created_at": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
    }
    (tmp_path / ".github/secret-exceptions.json").write_text(json.dumps({"exceptions": [item]}))
    monkeypatch.setattr(check_repository, "ROOT", tmp_path)
    monkeypatch.setattr(check_repository, "_tracked_files", lambda: [planted])

    check_repository.check_secrets(now)


def test_distribution_rejects_symlinks_and_native_payloads():
    with pytest.raises(SystemExit, match="symlink"):
        check_distribution.reject_payload("package/link", symlink=True)
    with pytest.raises(SystemExit, match="native"):
        check_distribution.reject_payload("package/addon.node")


@pytest.mark.skipif(os.name == "nt", reason="sentinel wrapper uses a POSIX executable")
def test_repository_command_does_not_inherit_ambient_secret(tmp_path):
    wrapper = tmp_path / "probe"
    wrapper.write_text('#!/bin/sh\nif [ -n "$SENTINEL_RELEASE_SECRET" ]; then exit 97; fi\nexit 0\n')
    wrapper.chmod(0o755)
    os.environ["SENTINEL_RELEASE_SECRET"] = "must-not-reach-repository-code"  # pragma: allowlist secret
    try:
        check_repository._run(str(wrapper))
    finally:
        os.environ.pop("SENTINEL_RELEASE_SECRET")


def test_repository_command_rejects_planted_secret_in_temporary_repo(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for name in (
        "CODE_OF_CONDUCT.md",
        "MAINTAINERS.md",
        "SUPPORT.md",
        "SECURITY.md",
        ".github/CODEOWNERS",
        ".github/pull_request_template.md",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == ".github/CODEOWNERS":
            path.write_text(
                "/.github/workflows/ci.yml @kimpton-ai/security\n"
                "/scripts/classify_ci_changes.py @kimpton-ai/security\n"
            )
        else:
            path.write_text("synthetic policy fixture\n")
    license_text = (root / "LICENSE").read_text()
    (tmp_path / "LICENSE").write_text(license_text)
    package = tmp_path / "packages/typescript"
    package.mkdir(parents=True)
    (package / "LICENSE").write_text(license_text)
    (package / "package.json").write_text(
        json.dumps({"version": "0.2.0", "license": "MIT", "files": ["LICENSE"]})
    )
    (package / "package-lock.json").write_text(
        json.dumps({"version": "0.2.0", "packages": {"": {"version": "0.2.0"}}})
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="0.2.0"\nlicense="MIT"\nlicense-files=["LICENSE"]\n'
    )
    python_package = tmp_path / "src/environment_harness"
    python_package.mkdir(parents=True)
    (python_package / "__init__.py").write_text('__version__ = "0.2.0"\n')
    (tmp_path / ".github/secret-exceptions.json").write_text('{"exceptions": []}')
    (tmp_path / "planted.txt").write_text("credential=" + "AKIA" + "C" * 16)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)

    result = subprocess.run(
        [sys.executable, str(root / "scripts/check_repository.py"), "--root", str(tmp_path)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "AWS access key" in result.stderr
