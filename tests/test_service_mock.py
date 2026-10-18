"""End to end against the contract mock (contract/mock/server.py): quote, start, wait, fetch.

Blender is replaced by a collector that writes a staging folder, so these run anywhere; the
real Blender pack is covered by test_blender_pack.py.
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from loopcut_render import pack, service, state

MOCK = Path(__file__).resolve().parents[3] / "contract" / "mock" / "server.py"


def _mock_module():
    spec = importlib.util.spec_from_file_location("loopcut_contract_mock", MOCK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_collect(blend, staging, blender):
    staging = Path(staging)
    (staging / "scene.blend").write_bytes(Path(blend).read_bytes())
    (staging / "textures" / "a1b2").mkdir(parents=True)
    (staging / "textures" / "a1b2" / "wood.png").write_bytes(os.urandom(300_000))  # Over the multipart minimum below.
    return {"blender": "5.2.2", "scene": {"name": "Scene", "camera": "Camera", "frame_start": 1, "frame_end": 48, "fps": 24.0},
            "created_at": "2026-10-18T12:00:00Z", "missing": [{"type": "textures", "name": "rock", "path": "//rock.png"}],
            "unsupported": [], "system": []}


@unittest.skipUnless(MOCK.is_file(), "contract mock not available outside loopcut-web")
class MockEndToEnd(unittest.TestCase):
    def setUp(self):
        module = _mock_module()
        self.server = module.create_server("127.0.0.1", 0, job_seconds=0.5, live_tools=["render"],
                                           multipart_min_size=256 * 1024, multipart_part_size=128 * 1024)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {"LOOPCUT_RENDER_HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = Path(self.home.name).resolve() / "project"
        self.work.mkdir()
        self.blend = self.work / "shot.blend"
        self.blend.write_bytes(b"BLENDER-v502" + os.urandom(1000))
        self.service = service.Service(base_url=f"http://127.0.0.1:{self.server.server_port}", api_key="lc_test")

    def quote(self, **settings):
        with mock.patch.object(pack, "collect", fake_collect), mock.patch.object(pack, "find_blender", return_value="/bin/true"):
            return self.service.quote(self.blend, {"frame_start": 1, "frame_end": 3, **settings})

    def test_quote_start_wait_fetch(self):
        quote = self.quote(samples=32)
        self.assertEqual(quote["status"], "quoted")
        self.assertEqual(quote["frames"], 3)
        self.assertEqual(quote["scene"]["frame_end"], 48)
        self.assertEqual(quote["warnings"], ["Missing textures: //rock.png"])
        job = self.service.start(quote["quote_id"])
        self.assertTrue(job["job_id"].startswith("job_"))
        self.assertEqual(self.service.start(quote["quote_id"])["job_id"], job["job_id"], "a repeated start is the same job")
        done = self.service.status(job["job_id"], wait=20)
        self.assertEqual(done["status"], "succeeded")
        fetched = self.service.fetch(job["job_id"])
        out = self.work / "loopcut_renders" / job["job_id"]
        self.assertEqual(fetched["out_dir"], str(out))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["frame_0001.png", "frame_0002.png", "frame_0003.png", "report.json"])
        self.assertEqual(fetched["files_pending"], 0)
        self.assertIn("qa", fetched)
        again = self.service.fetch(job["job_id"])
        self.assertEqual(again["files_saved"], 4, "verified files are kept, not downloaded again")

    def test_manifest_blob_is_canonical_and_multipart_used(self):
        quote = self.quote()
        record = state.load("quotes", quote["quote_id"])
        blobs = self.server.state.blobs
        self.assertIn(record["body"]["bundle"], blobs)
        self.assertEqual(len(self.server.state.uploads), 0, "multipart uploads completed")
        self.assertEqual(quote["bundle"]["files"], 2)

    def test_expired_quote_is_renewed_at_the_same_price(self):
        quote = self.quote()
        record = state.load("quotes", quote["quote_id"])
        state.save("quotes", quote["quote_id"], {**record, "expires_at": "2020-01-01T00:00:00Z"})
        job = self.service.start(quote["quote_id"])
        self.assertTrue(job["job_id"].startswith("job_"))

    def test_cancel(self):
        quote = self.quote(frame_end=40)
        job = self.service.start(quote["quote_id"])
        canceled = self.service.cancel(job["job_id"])
        self.assertIn(canceled["status"], {"canceled", "succeeded"})

    def test_signed_out(self):
        signed_out = service.Service(base_url=self.service.base_url)
        with mock.patch.dict(os.environ, {"LOOPCUT_API_KEY": ""}):
            with self.assertRaises(service.RenderError) as caught:
                signed_out.account()
        self.assertEqual(caught.exception.code, "sign_in_required")

    def test_account(self):
        account = self.service.account()
        self.assertEqual(account["render"]["available"], True)
        self.assertIsInstance(account["credits"], int)


class Canonical(unittest.TestCase):
    def test_whole_floats_are_integers_and_keys_sorted(self):
        body = pack.canonical_json({"scene": {"fps": 24.0, "name": "Ä"}, "a": [1.5]})
        self.assertEqual(body, '{"a":[1.5],"scene":{"fps":24,"name":"Ä"}}'.encode("utf-8"))

    def test_manifest_uses_job_source_and_sorted_files(self):
        with tempfile.TemporaryDirectory() as staging:
            info = fake_collect(Path(__file__), staging, None)
            value = pack.manifest(staging, info)
        self.assertEqual(value["source"], {"kind": "job"})
        self.assertEqual([item["path"] for item in value["files"]], ["scene.blend", "textures/a1b2/wood.png"])
        self.assertEqual(value["files"][0]["sha256"], hashlib.sha256(Path(__file__).read_bytes()).hexdigest())

    @unittest.skipUnless(MOCK.is_file(), "contract schemas not available outside loopcut-web")
    def test_manifest_matches_contract_schema(self):
        module = _mock_module()
        registry = module.load_schemas(MOCK.parents[1] / "schemas")
        with tempfile.TemporaryDirectory() as staging:
            value = pack.manifest(staging, fake_collect(Path(__file__), staging, None))
        self.assertEqual(module.validate(json.loads(pack.canonical_json(value)), "bundle-manifest", registry), [])


if __name__ == "__main__":
    sys.exit(unittest.main())
