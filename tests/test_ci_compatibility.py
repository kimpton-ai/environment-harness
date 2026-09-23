import importlib.util
import sys
from pathlib import Path


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "classify_ci_changes.py"
    spec = importlib.util.spec_from_file_location("classify_ci_changes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["classify_ci_changes"] = module
    spec.loader.exec_module(module)
    return module


def file_change(filename, *, previous_filename=None):
    change = {"filename": filename}
    if previous_filename is not None:
        change["previous_filename"] = previous_filename
    return change


def test_documentation_only_change_skips_compatibility_matrix():
    classifier = load_script()
    files = [file_change("docs/COMPATIBILITY.md")]

    assert classifier.requires_compatibility(files, changed_files=1) is False


def test_viewer_only_change_skips_compatibility_matrix():
    classifier = load_script()
    files = [file_change("src/environment_harness/viewer/app.ts")]

    assert classifier.requires_compatibility(files, changed_files=1) is False


def test_python_source_change_runs_compatibility_matrix():
    classifier = load_script()
    files = [file_change("src/environment_harness/runtime.py")]

    assert classifier.requires_compatibility(files, changed_files=1) is True


def test_schema_integration_viewer_and_documentation_jobs_are_classified_independently():
    classifier = load_script()

    assert classifier.classify_impacts(
        [file_change("contracts/Trajectory.schema.json")], changed_files=1
    ) == {
        "compatibility": True,
        "schema": True,
        "integrations": False,
        "viewer": False,
        "documentation": False,
    }
    assert (
        classifier.classify_impacts(
            [file_change("src/environment_harness/adapters/frameworks.py")], changed_files=1
        )["integrations"]
        is True
    )
    assert classifier.classify_impacts([file_change("packages/typescript/src/app.ts")], changed_files=1) == {
        "compatibility": False,
        "schema": False,
        "integrations": False,
        "viewer": True,
        "documentation": False,
    }
    assert (
        classifier.classify_impacts([file_change("docs/TRAINING.md")], changed_files=1)["documentation"]
        is True
    )


def test_impact_classification_fails_closed_on_incomplete_metadata():
    classifier = load_script()

    assert all(classifier.classify_impacts([file_change("docs/one.md")], changed_files=2).values())


def test_rename_from_sensitive_path_runs_compatibility_matrix():
    classifier = load_script()
    files = [
        file_change(
            "docs/runtime-example.py",
            previous_filename="src/environment_harness/runtime.py",
        )
    ]

    assert classifier.requires_compatibility(files, changed_files=1) is True


def test_rename_to_sensitive_path_runs_compatibility_matrix():
    classifier = load_script()
    files = [
        file_change(
            "src/environment_harness/runtime.py",
            previous_filename="docs/runtime-example.py",
        )
    ]

    assert classifier.requires_compatibility(files, changed_files=1) is True


def test_incomplete_github_file_list_fails_closed():
    classifier = load_script()
    returned_files = [file_change(f"docs/example-{index}.md") for index in range(3_000)]

    assert classifier.requires_compatibility(returned_files, changed_files=3_001) is True


def test_malformed_github_file_record_fails_closed():
    classifier = load_script()

    assert classifier.requires_compatibility([{"filename": None}], changed_files=1) is True


def test_paginated_github_response_is_flattened():
    classifier = load_script()
    pages = [
        [file_change("docs/one.md")],
        [file_change("src/environment_harness/store.py")],
    ]

    assert classifier.flatten_pages(pages) == [pages[0][0], pages[1][0]]
