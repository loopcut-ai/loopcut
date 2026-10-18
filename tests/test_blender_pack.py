"""Pack a real scene with a real Blender (heavy: set LOOPCUT_TEST_BLENDER to run it)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from loopcut import pack

BLENDER = os.environ.get("LOOPCUT_TEST_BLENDER")


@unittest.skipUnless(BLENDER, "set LOOPCUT_TEST_BLENDER to a Blender executable")
class BlenderPack(unittest.TestCase):
    def test_collects_external_texture_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as staging:
            work = Path(folder)
            subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1",
                            "--python", str(Path(__file__).with_name("make_scene.py")), "--", str(work)],
                           check=True, capture_output=True, timeout=300)
            info = pack.collect(work / "shot.blend", staging, pack.find_blender(BLENDER))
            value = pack.manifest(staging, info)
        paths = [item["path"] for item in value["files"]]
        self.assertEqual(paths[0], "scene.blend")
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[1].startswith("textures/") and paths[1].endswith("/checker.png"), paths)
        self.assertEqual(value["scene"]["frame_end"], 24)
        self.assertEqual([item["path"] for item in info["missing"]], ["//tex/gone.png"])
        self.assertEqual(value["source"], {"kind": "job"})


if __name__ == "__main__":
    unittest.main()
