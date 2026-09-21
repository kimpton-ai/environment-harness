import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
subprocess.run(["npm", "run", "build"], cwd=root / "packages/typescript", check=True)
for name in ("app.js", "timeline.js", "client.js", "types.js"):
    source = root / "packages/typescript/dist" / name
    destination = root / "src/environment_harness/viewer" / name
    if "--check" in sys.argv:
        if not destination.exists() or source.read_bytes() != destination.read_bytes():
            raise SystemExit("viewer drift: " + name)
    else:
        shutil.copyfile(source, destination)
