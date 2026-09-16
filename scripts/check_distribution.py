"""Inspect built artifacts for explicit distribution boundaries."""

import tarfile
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for artifact in (root / "dist").iterdir():
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
    elif artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact) as archive:
            names = archive.getnames()
    else:
        continue
    forbidden = (
        ".env",
        ".sqlite",
        ".local/",
        "researcher-token",
        "node_modules/",
        "marketbench_core/",
        "civilization.py",
        "environment_sessions.py",
    )
    bad = [name for name in names if any(part in name for part in forbidden)]
    if bad:
        raise SystemExit("private or generated runtime content in distribution: " + ", ".join(bad))
    if not any(name.endswith("viewer/app.js") for name in names):
        raise SystemExit("viewer missing from distribution")
    print(artifact.name + ": distribution boundary passed")
