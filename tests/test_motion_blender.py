"""motion_apply with a real Blender: the contract mock's SOMA walk onto a Mixamo-named rig in a saved .blend.

Set LOOPCUT_TEST_BLENDER to a Blender executable to run it (one Blender at a time on small machines)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from loopcut import service

BLENDER = os.environ.get("LOOPCUT_TEST_BLENDER")
HERE = Path(__file__).resolve().parent
WALK = HERE.parents[2] / "contract" / "mock" / "motion" / "kimodo_walk_60.bvh"


@unittest.skipUnless(BLENDER and WALK.is_file(), "set LOOPCUT_TEST_BLENDER (loopcut-web checkout) to run Blender")
class MotionApplyInBlender(unittest.TestCase):
    def test_walk_goes_onto_the_only_rig_and_the_file_is_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            blend = folder / "rig.blend"
            subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "--python",
                            str(HERE / "make_rig.py"), "--", str(blend)], check=True, capture_output=True, timeout=300)
            job = folder / "job_test"
            job.mkdir()
            shutil.copyfile(WALK, job / "motion.bvh")
            run = service.Service(base_url="http://127.0.0.1:9", api_key="lc_test")
            result = run.motion_apply(blend, job, blender=BLENDER, out=folder / "walk.blend", wait=None)
            self.assertEqual(result["status"], "applied")
            self.assertEqual((result["armature"], result["picked"]), ("Hero", "only humanoid armature in the scene"))
            self.assertIn("Mixamo", result["result"])
            self.assertTrue((folder / "walk.blend").is_file())
            in_place = run.motion_apply(blend, job / "motion.bvh", blender=BLENDER, armature="Hero body", in_place=True,
                                        wait=None)
            self.assertIn("in place", in_place["result"])
            self.assertTrue(Path(in_place["previous_version"]).is_file(), "Blender kept the file it replaced")
            with self.assertRaises(service.LoopcutError) as caught:
                run.motion_apply(blend, job, blender=BLENDER, armature="Ground", wait=None)
            self.assertEqual(caught.exception.code, "apply_failed")
            self.assertIn("'Ground' is not an armature", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
