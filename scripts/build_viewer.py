import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
subprocess.run(["npm", "run", "build"], cwd=root / "packages/typescript", check=True)
for name in ("app.js", "client.js", "types.js"):
    shutil.copyfile(root / "packages/typescript/dist" / name, root / "src/environment_harness/viewer" / name)
