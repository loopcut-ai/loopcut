"""loopcut.scene without Blender: references, ambientCG archives, and where assets come from."""

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from loopcut import scene


class References(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(scene.parse_ref("ambientcg:Bricks077"), ("ambientcg", "Bricks077"))
        self.assertEqual(scene.parse_ref("job:job_ab-c_1/model.glb"), ("job", ("job_ab-c_1", "model.glb")))
        for bad in ("polyhaven:brick", "ambientcg:../x", "job:job_x/../../etc", "job:job_x/a/b.glb", 3):
            with self.assertRaises(scene.AssetError):
                scene.parse_ref(bad)

    def test_ambientcg_names(self):
        self.assertEqual(scene.ambientcg_kind("DayEnvironmentHDRI001"), "hdri")
        self.assertEqual(scene.ambientcg_kind("Bricks077"), "material")
        self.assertEqual(scene.ambientcg_url("Bricks077", "2K", "material"), "https://ambientcg.com/get?file=Bricks077_2K-JPG.zip")
        self.assertEqual(scene.ambientcg_url("DayEnvironmentHDRI001", "1K", "hdri"), "https://ambientcg.com/get?file=DayEnvironmentHDRI001_1K.zip")
        self.assertEqual(scene.index_key("ambientcg:Bricks077", "2K"), "ambientcg:Bricks077@2K")
        self.assertEqual(scene.index_key("job:job_x/a.glb"), "job:job_x/a.glb")


class Downloads(unittest.TestCase):
    def test_redirects_stay_on_ambientcg_over_https(self):
        handler = scene._AmbientcgRedirects()
        request = mock.Mock(full_url="https://ambientcg.com/get?file=x.zip", headers={}, get_method=lambda: "GET")
        for url in ("https://attacker.test/x.zip", "http://acg-download.struffelproductions.com/x.zip",
                    "https://acg-download.struffelproductions.com:8443/x.zip"):
            with self.assertRaises(scene.AssetError):
                handler.redirect_request(request, None, 302, "Found", {}, url)


class Archives(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _zip(self, names):
        path = self.tmp / "asset.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name in names:
                zf.writestr(name, b"data")
        return path

    def test_material_maps_only(self):
        archive = self._zip(["Bricks077_1K-JPG_Color.jpg", "Bricks077_1K-JPG_NormalGL.jpg", "Bricks077_1K-JPG_Roughness.jpg",
                             "Bricks077_1K-JPG.blend", "Bricks077_1K-JPG.usdc", "../../escape_Color.jpg", "nested/dir/Bricks077_1K-JPG_Metalness.jpg"])
        entry = scene.extract_maps(archive, self.tmp / "out")
        self.assertEqual(sorted(entry["maps"]), ["color", "metalness", "normalgl", "roughness"])
        # Only maps, each under its base name: a crafted "../../" path lands inside the folder too.
        self.assertEqual(sorted(os.listdir(self.tmp / "out")), ["Bricks077_1K-JPG_Color.jpg", "Bricks077_1K-JPG_Metalness.jpg",
                                                                "Bricks077_1K-JPG_NormalGL.jpg", "Bricks077_1K-JPG_Roughness.jpg", "escape_Color.jpg"])
        self.assertTrue(entry["maps"]["color"].endswith("Bricks077_1K-JPG_Color.jpg"))
        for path in entry["maps"].values():
            self.assertEqual(Path(path).parent, self.tmp / "out")
        self.assertFalse((self.tmp / "escape_Color.jpg").exists())

    def test_hdri(self):
        entry = scene.extract_maps(self._zip(["Day_1K_HDR.exr", "Day_1K_TONEMAPPED.jpg", "Day_1K.blend"]), self.tmp / "out")
        self.assertEqual(entry, {"file": str(self.tmp / "out" / "Day_1K_HDR.exr")})

    def test_nothing_usable(self):
        with self.assertRaises(scene.AssetError):
            scene.extract_maps(self._zip(["readme.txt", "x.blend"]), self.tmp / "out")


class Resolve(unittest.TestCase):
    def setUp(self):
        scene._standins.clear()
        self.tmp = Path(tempfile.mkdtemp())

    def test_builder_index_only(self):
        index = self.tmp / "index.json"
        index.write_text(json.dumps({"assets": {"ambientcg:Bricks077@2K": {"maps": {"color": "/x.jpg"}}, "job:job_a/m.glb": {"file": "/m.glb"}}}))
        with mock.patch.dict(os.environ, {scene.ASSETS_ENV: str(index)}), mock.patch.object(scene, "_download") as download:
            self.assertEqual(scene._resolve("ambientcg:Bricks077", "2K", "material"), {"maps": {"color": "/x.jpg"}})
            self.assertEqual(scene._resolve("job:job_a/m.glb", None, "file"), {"file": "/m.glb"})
            with self.assertRaisesRegex(scene.AssetError, "list it in the render call's assets"):
                scene._resolve("ambientcg:Bricks077", "1K", "material")
            download.assert_not_called()

    def test_sandbox_stand_ins(self):
        env = {key: value for key, value in os.environ.items() if key != scene.ASSETS_ENV}
        env["LOOPCUT_SCENE_CACHE"] = str(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(scene, "_download", side_effect=OSError("blocked")):
            self.assertIsNone(scene._resolve("ambientcg:Bricks077", "2K", "material"))
            self.assertIsNone(scene._resolve("job:job_a/m.glb", None, "file"))
        self.assertEqual(scene.standins(), ["ambientcg:Bricks077", "job:job_a/m.glb"])


if __name__ == "__main__":
    unittest.main()
