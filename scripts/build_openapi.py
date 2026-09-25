"""Generate the checked-in OpenAPI document from the FastAPI application."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from environment_harness import EvidenceStore
from environment_harness.fixtures import SyntheticEnvironment
from environment_harness.runtime import _SessionRuntime
from environment_harness.server import create_app

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "contracts" / "openapi.json"


def render() -> str:
    with tempfile.TemporaryDirectory(prefix="environment-harness-openapi-") as directory:
        session = _SessionRuntime(EvidenceStore(directory), SyntheticEnvironment())
        document = create_app(session).openapi()
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main() -> None:
    content = render()
    if "--check" in sys.argv:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            raise SystemExit("OpenAPI drift: run `uv run python scripts/build_openapi.py`")
        print("OpenAPI contract is current")
        return
    OUTPUT.write_text(content)
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
