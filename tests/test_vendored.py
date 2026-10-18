"""The vendored add-on modules must stay identical to the add-on (addon/VENDORED.md)."""

import os
import unittest
from pathlib import Path

VENDORED = Path(__file__).resolve().parents[1] / "src" / "loopcut_render" / "addon"
ADDON = os.environ.get("LOOPCUT_ADDON_DIR")


@unittest.skipUnless(ADDON, "set LOOPCUT_ADDON_DIR to a loopcut/blender checkout to compare")
class Vendored(unittest.TestCase):
    def test_copies_match_the_addon(self):
        source = Path(ADDON) / "scripts" / "addons_core" / "loopcut"
        for name in ("bundles.py", "blobs.py", "cloud_http.py"):
            with self.subTest(name):
                self.assertEqual((VENDORED / name).read_bytes(), (source / name).read_bytes(),
                                 f"{name} differs from the add-on: run scripts/sync_addon.py")


if __name__ == "__main__":
    unittest.main()
