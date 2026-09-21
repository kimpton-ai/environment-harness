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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", type=Path, help="slurped response from the pull-request files API")
    parser.add_argument("--changed-files", required=True, type=int)
    args = parser.parse_args()

    document = json.loads(args.files.read_text())
    files = flatten_pages(document)
    result = requires_compatibility(files, changed_files=args.changed_files)
    print(str(result).lower())


if __name__ == "__main__":
    main()
