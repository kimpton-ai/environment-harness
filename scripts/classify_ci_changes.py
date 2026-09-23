"""Classify whether a pull request requires the compatibility matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

VIEWER_PREFIX = "src/environment_harness/viewer/"
COMPATIBILITY_PREFIXES = ("src/", "tests/", "examples/", "scripts/", "contracts/")
COMPATIBILITY_FILES = {
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    ".github/workflows/ci.yml",
}
IMPACT_NAMES = ("compatibility", "schema", "integrations", "viewer", "documentation")


def flatten_pages(document: Any) -> list[Any]:
    """Flatten the page arrays produced by ``gh api --paginate --slurp``."""
    if not isinstance(document, list):
        raise ValueError("GitHub file response must be a list")
    if not document:
        return []
    if all(isinstance(page, list) for page in document):
        return [item for page in document for item in page]
    if any(isinstance(page, list) for page in document):
        raise ValueError("GitHub file response mixes pages and file records")
    return document


def _path_requires_compatibility(path: str) -> bool:
    if path.startswith(VIEWER_PREFIX):
        return False
    return path in COMPATIBILITY_FILES or path.startswith(COMPATIBILITY_PREFIXES)


def requires_compatibility(files: list[Any], *, changed_files: int) -> bool:
    """Return true for sensitive paths and whenever GitHub data is incomplete."""
    if changed_files < 0 or len(files) != changed_files:
        return True

    for change in files:
        if not isinstance(change, dict):
            return True
        filename = change.get("filename")
        previous_filename = change.get("previous_filename")
        if not isinstance(filename, str):
            return True
        if previous_filename is not None and not isinstance(previous_filename, str):
            return True
        paths = (filename,) if previous_filename is None else (filename, previous_filename)
        if any(_path_requires_compatibility(path) for path in paths):
            return True
    return False


def classify_impacts(files: list[Any], *, changed_files: int) -> dict[str, bool]:
    """Classify path-routed jobs, failing closed when GitHub metadata is incomplete."""
    if changed_files < 0 or len(files) != changed_files:
        return dict.fromkeys(IMPACT_NAMES, True)
    impacts = dict.fromkeys(IMPACT_NAMES, False)
    for change in files:
        if not isinstance(change, dict):
            return dict.fromkeys(IMPACT_NAMES, True)
        filename = change.get("filename")
        previous_filename = change.get("previous_filename")
        if not isinstance(filename, str) or (
            previous_filename is not None and not isinstance(previous_filename, str)
        ):
            return dict.fromkeys(IMPACT_NAMES, True)
        paths = (filename,) if previous_filename is None else (filename, previous_filename)
        for path in paths:
            impacts["compatibility"] |= _path_requires_compatibility(path)
            impacts["schema"] |= path.startswith("contracts/") or path in {
                "scripts/build_contracts.py",
                "scripts/build_openapi.py",
                "src/environment_harness/contracts.py",
                "src/environment_harness/trajectories.py",
                "src/environment_harness/training.py",
                "src/environment_harness/server.py",
            }
            impacts["integrations"] |= path.startswith("src/environment_harness/adapters/") or path in {
                "src/environment_harness/plugins.py",
                "src/environment_harness/training.py",
                "pyproject.toml",
                "uv.lock",
            }
            impacts["viewer"] |= (
                path.startswith(VIEWER_PREFIX)
                or path.startswith("packages/typescript/")
                or path in {"scripts/build_viewer.py", "scripts/browser_ui_test.mjs"}
            )
            impacts["documentation"] |= path.startswith("docs/") or path in {
                "README.md",
                "CHANGELOG.md",
            }
    return impacts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", type=Path, help="slurped response from the pull-request files API")
    parser.add_argument("--changed-files", required=True, type=int)
    parser.add_argument("--impact", choices=IMPACT_NAMES, default="compatibility")
    args = parser.parse_args()

    document = json.loads(args.files.read_text())
    files = flatten_pages(document)
    result = classify_impacts(files, changed_files=args.changed_files)[args.impact]
    print(str(result).lower())


if __name__ == "__main__":
    main()
