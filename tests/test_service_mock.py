"""End to end against the contract mock (contract/mock/server.py): the catalog, render, a free tool,
a paid tool with a file input, and the fallbacks when the server can't be read.

Blender is replaced by a collector that writes a staging folder, so these run anywhere; the
real Blender pack is covered by test_blender_pack.py.
"""

import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import time
import urllib.request

from loopcut import catalog, mcp_server, pack, service, state

MOCK = Path(__file__).resolve().parents[3] / "contract" / "mock" / "server.py"
# The mock takes any lc_ bearer as its account, except ones starting lc_revoked (a revoked key).
REVOKED = "lc_" + "revoked"
LIVE = ["render", "asset_search", "image_to_3d", "retopology", "sfx", "voice_line"]
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(2000)


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


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class JobResultTests(unittest.TestCase):
    def test_partial_job_tells_the_model_how_to_finish(self):
        partial = {"requested": 288, "delivered": 200, "cause": "input", "resume_input": {"frame_start": 201},
                   "message": "Rendered 200 of 288 frames; Blender stopped with an error while rendering frame 201."}
        job = {"id": "job_x", "tool": "render", "status": "succeeded", "progress": {"pct": 100, "message": "Done"},
               "credits": {"reserved": 221, "charged": 11, "refunded": 210}, "outputs": [], "error": None, "partial": partial}
        result = service.Service._job_result(job)
        self.assertEqual(result["partial"], partial)
        self.assertTrue(result["next"].startswith("Only part finished: Rendered 200 of 288 frames"))
        self.assertIn("partial.resume_input", result["next"])
        self.assertNotIn("partial", service.Service._job_result({**job, "partial": None}))


@unittest.skipUnless(MOCK.is_file(), "contract mock not available outside loopcut-web")
class MockEndToEnd(unittest.TestCase):
    def setUp(self):
        module = _mock_module()
        self.server = module.create_server("127.0.0.1", 0, job_seconds=0.5, live_tools=LIVE,
                                           multipart_min_size=256 * 1024, multipart_part_size=128 * 1024)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {"LOOPCUT_CONNECTOR_HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = Path(self.home.name).resolve() / "project"
        self.work.mkdir()
        previous = os.getcwd()
        os.chdir(self.work)  # Results land in loopcut_outputs/ under the working folder.
        self.addCleanup(os.chdir, previous)
        self.blend = self.work / "shot.blend"
        self.blend.write_bytes(b"BLENDER-v502" + os.urandom(1000))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.service = service.Service(base_url=self.url, api_key="lc_test")

    def quote(self, **settings):
        with mock.patch.object(pack, "collect", fake_collect), mock.patch.object(pack, "find_blender", return_value="/bin/true"):
            return self.service.quote(self.blend, {"frame_start": 1, "frame_end": 3, **settings})

    # ------------------------------------------------------------------ render

    def test_render_quote_start_wait_fetch(self):
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
        self.assertIn("qa", fetched)
        again = self.service.fetch(job["job_id"])
        self.assertEqual(again["files_saved"], 4, "verified files are kept, not downloaded again")
        self.assertNotEqual(self.quote(samples=32)["quote_id"], quote["quote_id"], "a used quote is not handed out again")

    def test_manifest_blob_is_canonical_and_multipart_used(self):
        quote = self.quote()
        record = state.load("quotes", quote["quote_id"])
        self.assertIn(record["body"]["bundle"], self.server.state.blobs)
        self.assertEqual(len(self.server.state.uploads), 0, "multipart uploads completed")
        self.assertEqual(quote["bundle"]["files"], 2)

    def test_expired_quote_is_renewed_at_the_same_price(self):
        quote = self.quote()
        record = state.load("quotes", quote["quote_id"])
        state.save("quotes", quote["quote_id"], {**record, "expires_at": "2020-01-01T00:00:00Z"})
        job = self.service.start(quote["quote_id"])
        self.assertTrue(job["job_id"].startswith("job_"))
        self.assertEqual(self.service.start(quote["quote_id"])["job_id"], job["job_id"], "the renewed quote's job, not a second one")

    def test_cancel(self):
        quote = self.quote(frame_end=40)
        job = self.service.start(quote["quote_id"])
        canceled = self.service.cancel(job["job_id"])
        self.assertIn(canceled["status"], {"canceled", "succeeded"})

    # ------------------------------------------------------------------ other tools

    def test_free_asset_search_runs_straight_through(self):
        result = self.service.quote_tool("asset_search", {"query": "arm chair", "kind": "model"}, wait=20)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["tool"], "asset_search")
        self.assertGreaterEqual(result["count"], 1)
        chair = next(row for row in result["results"] if row["id"] == "polyhaven:ArmChair_01")
        self.assertEqual(chair["license"], "CC0-1.0")
        self.assertEqual(chair["files_api"], "https://api.polyhaven.com/files/ArmChair_01")
        self.assertIn("Powered by Poly Haven", result["show_credit"])
        self.assertEqual(Path(result["report"]).parent, self.work / "loopcut_outputs" / result["job_id"])

    def test_paid_tool_uploads_its_file_and_downloads_the_model(self):
        image = self.work / "lamp.png"
        image.write_bytes(PNG)
        quote = self.service.quote_tool("image_to_3d", {"prompt": "a brass desk lamp", "quality": "draft"},
                                        {"image": str(image)}, wait=20)
        self.assertEqual(quote["status"], "quoted", "a paid tool stops at the quote")
        self.assertGreater(quote["credits"], 0)
        record = state.load("quotes", quote["quote_id"])
        digest = hashlib.sha256(PNG).hexdigest()
        self.assertEqual(record["body"]["files"], {"image": digest})
        self.assertIn(digest, self.server.state.blobs, "the image was uploaded by hash")
        result = self.service.start(quote["quote_id"], wait=20)
        self.assertEqual(result["status"], "succeeded")
        model = Path(result["files"][0]["path"])
        self.assertEqual((model.name, model.parent), ("model.glb", self.work / "loopcut_outputs" / result["job_id"]))
        self.assertTrue(model.read_bytes().startswith(b"glTF"))
        self.assertIn("import_scene.gltf", result["next"])
        self.assertEqual(self.service.start(quote["quote_id"], wait=0)["job_id"], result["job_id"])

    def test_file_inputs_are_checked_before_upload(self):
        text = self.work / "notes.png"
        text.write_text("not an image")
        with self.assertRaises(service.LoopcutError) as caught:
            self.service.quote_tool("image_to_3d", {}, {"image": str(text)}, wait=5)
        self.assertEqual((caught.exception.code, caught.exception.extra["field"]), ("invalid_input", "image"))
        with self.assertRaises(service.LoopcutError) as caught:
            self.service.quote_tool("remesh", {"target_faces": 4000}, {}, wait=5)
        self.assertEqual(caught.exception.extra["field"], "model")

    def test_private_mode_refusal_says_what_to_do(self):
        with self.assertRaises(service.LoopcutError) as caught:
            self.service.quote_tool("sound_effect", {"prompt": "a door slams"}, wait=5)
        self.assertEqual(caught.exception.code, "private_mode")
        self.assertIn("Private mode is on", str(caught.exception))
        self.assertIn("loopcut settings --private-mode off", str(caught.exception))
        self.server.state.settings["private_mode"] = False
        self.assertEqual(self.service.quote_tool("sound_effect", {"prompt": "a door slams"}, wait=5)["status"], "quoted")

    # ------------------------------------------------------------------ catalog and account

    def test_catalog_drives_the_tool_list(self):
        known = self.service.catalog()
        self.assertEqual(known.source, "server")
        self.assertEqual({tool["name"] for tool in known.visible()}, set(LIVE))
        self.server.state.live_tools.add("image_generate")
        seen = []
        self.service.on_catalog_change = seen.append
        self.assertIn("image_generate", {tool["name"] for tool in self.service.catalog(refresh=True).visible()})
        self.assertEqual(len(seen), 1)
        self.server.state.settings["no_generation"] = True
        visible = {tool["name"] for tool in self.service.catalog(refresh=True).visible()}
        self.assertNotIn("image_generate", visible, "no-generation hides generative tools")
        self.assertIn("render", visible)
        with self.assertRaises(service.LoopcutError) as caught:
            self.service.quote_tool("voice_line", {"text": "Hello"}, wait=5)
        self.assertEqual(caught.exception.code, "generation_disabled")

    def test_catalog_falls_back_to_the_last_read_then_the_snapshot(self):
        self.assertEqual(self.service.catalog().source, "server")
        offline = service.Service(base_url=self.url.rsplit(":", 1)[0] + f":{closed_port()}", api_key="lc_test")
        self.assertEqual(offline.catalog().source, "snapshot")
        self.assertEqual({tool["name"] for tool in offline.catalog().visible()},
                         {tool["name"] for tool in catalog.Catalog.from_snapshot().visible()})
        self.server.shutdown()
        self.server.server_close()
        restarted = service.Service(base_url=self.url, api_key="lc_test")
        self.assertEqual(restarted.catalog().source, "cache")
        self.assertEqual({tool["name"] for tool in restarted.catalog().visible()}, set(LIVE))

    # ------------------------------------------------------------------ signing in from any tool

    def signed_out(self):
        patcher = mock.patch.dict(os.environ, {"LOOPCUT_API_KEY": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        return service.Service(base_url=self.url)

    def approve(self, user_code=None, after=0.0):
        def run():
            time.sleep(after)
            code = user_code or next(entry["user_code"] for entry in self.server.state.devices.values()
                                     if entry["status"] == "pending")
            request = urllib.request.Request(self.url + "/mock/control", json.dumps({"approve_device": code}).encode(),
                                             {"content-type": "application/json"}, method="POST")
            urllib.request.urlopen(request, timeout=10).read()
        if not after:
            return run()
        threading.Thread(target=run, daemon=True).start()

    def test_a_signed_out_call_signs_in_and_carries_on(self):
        signed_out = self.signed_out()
        with mock.patch.object(service.webbrowser, "open", return_value=True) as opened:
            self.approve(after=1.5)
            result = mcp_server.call_tool(signed_out, "asset_search", {"query": "arm chair"}, budget=30)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertGreaterEqual(result["count"], 1)
        opened.assert_called_once()
        self.assertTrue(opened.call_args.args[0].startswith(self.url + "/link?code="))
        self.assertTrue(state.credentials(self.url)["api_key"].startswith("lc_mock_"), "the key is stored")

    def test_an_unapproved_sign_in_returns_the_link_and_is_reused(self):
        signed_out = self.signed_out()
        with mock.patch.object(service.webbrowser, "open", return_value=True) as opened:
            first = mcp_server.call_tool(signed_out, "loopcut_account", {}, budget=4)
            self.assertFalse(first["signed_in"])
            self.assertTrue(first["verification_url"].endswith(first["user_code"]))
            self.assertIn("call loopcut_account again", first["next"])
            second = mcp_server.call_tool(signed_out, "loopcut_account", {}, budget=4)
            self.assertEqual(second["user_code"], first["user_code"], "a retry keeps the same code")
            self.approve(first["user_code"])
            third = mcp_server.call_tool(signed_out, "loopcut_account", {}, budget=10)
        self.assertEqual(third["email"], "tester@example.com")
        self.assertEqual(opened.call_count, 1, "the browser opens once per sign-in")

    def test_without_a_browser_the_link_comes_back_at_once(self):
        signed_out = self.signed_out()
        started = time.monotonic()
        with mock.patch.object(service.webbrowser, "open", return_value=False):
            result = mcp_server.call_tool(signed_out, "job_status", {"job_id": "job_1", "wait_seconds": 40})
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("verification_url", result)

    def test_a_revoked_stored_key_restarts_sign_in(self):
        signed_out = self.signed_out()
        state.save_credentials(self.url, REVOKED + "_stored", "old@example.com")
        with mock.patch.object(service.webbrowser, "open", return_value=True):
            self.approve(after=1.5)
            result = mcp_server.call_tool(signed_out, "loopcut_account", {}, budget=30)
        self.assertEqual(result["email"], "tester@example.com")
        self.assertFalse(state.credentials(self.url)["api_key"].startswith(REVOKED))

    def test_a_given_api_key_is_never_replaced_by_a_sign_in(self):
        given = service.Service(base_url=self.url, api_key=REVOKED + "_given")
        with mock.patch.object(service.webbrowser, "open") as opened:
            with self.assertRaises(service.LoopcutError) as caught:
                mcp_server.call_tool(given, "loopcut_account", {})
        self.assertEqual(caught.exception.code, "invalid_api_key")
        opened.assert_not_called()

    def test_signed_out(self):
        signed_out = service.Service(base_url=self.service.base_url)
        with mock.patch.dict(os.environ, {"LOOPCUT_API_KEY": ""}):
            with self.assertRaises(service.LoopcutError) as caught:
                signed_out.account()
            self.assertEqual(caught.exception.code, "sign_in_required")
            self.assertEqual(signed_out.catalog().source, "server", "the catalog is read signed out too")

    def test_account(self):
        account = self.service.account()
        self.assertIsInstance(account["credits"], int)
        self.assertEqual(account["settings"], {"private_mode": True, "no_generation": False})
        tools = {row["tool"]: row for row in account["tools"]}
        self.assertEqual(tools["sound_effect"]["unavailable_reason"], "private_mode")
        self.assertIn("sound_effect", account["notes"][0])


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
