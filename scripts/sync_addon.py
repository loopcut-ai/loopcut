"""Copy the vendored add-on modules from a loopcut/blender checkout (see addon/VENDORED.md)."""

import shutil
import subprocess
import sys
from pathlib import Path

FILES = ("bundles.py", "blobs.py", "cloud_http.py")
TARGET = Path(__file__).resolve().parents[1] / "src" / "loopcut_render" / "addon"


def main(checkout):
    source = Path(checkout).expanduser() / "scripts" / "addons_core" / "loopcut"
    for name in FILES:
        shutil.copyfile(source / name, TARGET / name)
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--short=12", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    print(f"Copied {', '.join(FILES)} from {commit}; update the commit in {TARGET / 'VENDORED.md'}.")


if __name__ == "__main__":
    main(sys.argv[1])
