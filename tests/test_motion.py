"""Motion tools (contract 0.7): video facts without Blender, client-only apply_args, the job result's
apply instructions, motion_apply and the vendored retargeting module. The real Blender apply is
test_motion_blender.py; the end to end against the contract mock is at the bottom (loopcut-web only)."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from loopcut import catalog, cli, mcp_server, media, motion, service, state
from loopcut.addon import motion_retarget

MOCK = Path(__file__).resolve().parents[3] / "contract" / "mock" / "server.py"
WALK = MOCK.parent / "motion" / "kimodo_walk_60.bvh"


def box(kind, payload, large=False):
    if large:
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mvhd(seconds, timescale=600, version=0):
    if version == 1:
        body = b"\x01\x00\x00\x00" + struct.pack(">QQIQ", 0, 0, timescale, round(seconds * timescale))
    else:
        body = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, timescale, round(seconds * timescale))
    return box(b"mvhd", body + b"\x00" * 80)


def track(seconds, handler=b"vide", timescale=30000, version=0):
    times = struct.pack(">QQIQ", 0, 0, timescale, round(seconds * timescale)) if version == 1 else \
        struct.pack(">IIII", 0, 0, timescale, round(seconds * timescale))
    mdhd = box(b"mdhd", bytes([version, 0, 0, 0]) + times + b"\x00" * 4)
    hdlr = box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 12 + b"Handler\x00")
    return box(b"trak", box(b"tkhd", b"\x00" * 84) + box(b"mdia", mdhd + hdlr))


def mp4(seconds, brand=b"isom", version=0, moov_first=False, media_bytes=4096, large=False, tracks=b""):
    ftyp = box(b"ftyp", brand + b"\x00\x00\x02\x00" + brand + b"mp41")
    moov = box(b"moov", mvhd(seconds, version=version) + box(b"trak", box(b"tkhd", b"\x00" * 84)) + tracks)
    mdat = box(b"mdat", os.urandom(media_bytes), large=large)
    return ftyp + (moov + mdat if moov_first else mdat + moov)


WEBM = bytes.fromhex("1a45dfa3") + b"\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04\x42\xf3\x81\x08\x42\x82\x84webm" \
    + b"\x00" * 64


class Media(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name, data):
        path = Path(self.dir.name) / name
        path.write_bytes(data)
        return path

    def test_sniffs_containers_from_their_bytes(self):
        self.assertEqual(media.sniff_video(mp4(1)[:64]), "video/mp4")
        self.assertEqual(media.sniff_video(mp4(1, brand=b"qt  ")[:64]), "video/quicktime")
        self.assertEqual(media.sniff_video(WEBM[:64]), "video/webm")
        self.assertEqual(media.sniff_video(WEBM[:64].replace(b"webm", b"mkvx")), "video/x-matroska")
        self.assertIsNone(media.sniff_video(b"\x89PNG\r\n\x1a\n" + b"\x00" * 56))
        self.assertEqual(service._sniff(self.write("clip.bin", mp4(1))), "video/mp4")
        self.assertEqual(service._sniff(self.write("fake.mp4", b"not a video at all")), "application/octet-stream")

    def test_reads_the_movie_header_wherever_it_is(self):
        for options in ({}, {"moov_first": True}, {"version": 1}, {"large": True}, {"brand": b"qt  "}):
            with self.subTest(**{k: str(v) for k, v in options.items()}):
                self.assertAlmostEqual(media.iso_seconds(self.write("a.mp4", mp4(10.5, **options))), 10.5, places=3)
        # The video track's own length wins over the movie header's (an audio track, an edit, rounding).
        tracks = track(9.9766, handler=b"soun", timescale=48000) + track(9.976633, version=1) + track(4.0)
        self.assertAlmostEqual(media.iso_seconds(self.write("t.mp4", mp4(10.01, tracks=tracks))), 9.976633, places=5)
        no_moov = self.write("b.mp4", box(b"ftyp", b"isom\x00\x00\x00\x00") + box(b"mdat", b"\x00" * 100))
        self.assertIsNone(media.iso_seconds(no_moov))
        truncated = self.write("c.mp4", mp4(3)[:-60])
        self.assertIsNone(media.iso_seconds(truncated))

    def test_capture_length_is_measured_and_limited(self):
        clip = self.write("dance.mp4", mp4(12.25))
        self.assertEqual(motion.capture_inputs({"video_seconds": 99}, clip, "video/mp4")["video_seconds"], 12.25,
                         "the measured length replaces a guessed one")
        self.assertNotIn("end_s", motion.capture_inputs({"end_s": 30}, clip, "video/mp4"), "an end past the clip is dropped")
        long = self.write("long.mp4", mp4(95))
        with self.assertRaises(motion.MotionError) as caught:
            motion.capture_inputs({}, long, "video/mp4")
        self.assertIn("start_s and end_s", str(caught.exception))
        self.assertEqual(motion.capture_inputs({"start_s": 30, "end_s": 80}, long, "video/mp4")["video_seconds"], 95)
        with self.assertRaises(motion.MotionError):
            motion.capture_inputs({"start_s": 96}, long, "video/mp4")

    def test_webm_needs_ffprobe_or_a_given_length(self):
        clip = self.write("take.webm", WEBM)
        with mock.patch.object(media.shutil, "which", return_value=None):
            with self.assertRaises(motion.MotionError) as caught:
                motion.capture_inputs({}, clip, "video/webm")
            self.assertIn("video_seconds", str(caught.exception))
            self.assertEqual(motion.capture_inputs({"video_seconds": 8}, clip, "video/webm")["video_seconds"], 8)


class Vendored(unittest.TestCase):
    @unittest.skipUnless(WALK.is_file(), "the contract mock's BVH is only in loopcut-web")
    def test_the_vendored_retargeter_reads_a_soma_bvh(self):
        bvh = motion_retarget.parse_bvh(WALK.read_text(encoding="utf-8"))
        self.assertEqual((round(bvh.fps), len(bvh.frames)), (30, 60))
        rig = motion_retarget.detect([(j.name, j.parent.name if j.parent else None) for j in bvh.joints
                                      if not j.name.endswith("End")])
        self.assertEqual(rig.kind, "soma")

    def test_the_blender_entry_compiles_and_finds_the_module(self):
        source = Path(motion.SCRIPT).read_text(encoding="utf-8")
        compile(source, str(motion.SCRIPT), "exec")
        self.assertTrue((Path(motion.SCRIPT).parent / "addon" / "motion_retarget.py").is_file())
        spec = importlib.util.spec_from_file_location("entry", motion.SCRIPT)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)  # No bpy at import: it is only needed when applying.
        self.assertIs(entry.retarget_module().MotionError.__mro__[1], ValueError)


class Results(unittest.TestCase):
    def test_a_motion_result_says_how_to_use_it(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "job_abc"
            out.mkdir()
            (out / "motion.bvh").write_text("HIERARCHY\nROOT Root\n", encoding="utf-8")
            (out / "motion.json").write_text(json.dumps({"source": "kimodo-soma-rp-v1.2", "fps": 30, "frames": 150,
                                                         "seconds": 5.0, "fingers_animated": False,
                                                         "warnings": ["fingers_static"]}), encoding="utf-8")
            result = motion.result_for([out / "motion.bvh", out / "motion.json"],
                                       {"target_armature": "Hero rig", "in_place": True}, "a person waves")
        self.assertEqual(result["motion"]["frames"], 150)
        self.assertEqual(result["apply"]["motion_apply"], {"bvh": str(out / "motion.bvh"), "target_armature": "Hero rig",
                                                           "in_place": True})
        quoted = '"Hero rig"' if os.name == "nt" else "'Hero rig'"
        self.assertIn(f"--armature {quoted} --in-place", result["apply"]["cli"])
        compile(result["apply"]["bpy"], "snippet", "exec")
        self.assertIn("armature='Hero rig'", result["apply"]["bpy"])
        compile(result["apply"]["plain_import"], "import", "eval")
        self.assertIn("fingers don't move", result["next"])
        self.assertIn("motion_apply", result["next"])

    def test_apply_args_are_split_from_the_inputs(self):
        descriptor = next(t for t in catalog.snapshot()["tools"] if t["name"] == "motion_from_text")
        inputs = {"prompt": "walk", "target_armature": "Rig", "in_place": False}
        self.assertEqual(service._apply_args(descriptor, inputs), {"target_armature": "Rig", "in_place": False})
        self.assertEqual(inputs, {"prompt": "walk"})
        for bad in ({"in_place": "yes"}, {"target_armature": ""}, {"target_armature": "x" * 64}):
            with self.subTest(bad=bad), self.assertRaises(service.LoopcutError):
                service._apply_args(descriptor, dict(bad))

    def test_motion_apply_checks_its_paths_before_starting_blender(self):
        with tempfile.TemporaryDirectory() as folder:
            blend, bvh = Path(folder) / "scene.blend", Path(folder) / "motion.bvh"
            run = service.Service(base_url="http://127.0.0.1:9", api_key="lc_test")
            with self.assertRaises(service.LoopcutError) as caught:
                run.motion_apply(blend, folder)
            self.assertIn("No motion.bvh", str(caught.exception))
            bvh.write_text("not a bvh", encoding="utf-8")
            with self.assertRaises(service.LoopcutError) as caught:
                run.motion_apply(blend, bvh)
            self.assertIn("not a BVH", str(caught.exception))
            bvh.write_text("HIERARCHY\n", encoding="utf-8")
            with self.assertRaises(service.LoopcutError) as caught:
                run.motion_apply(blend, bvh)
            self.assertIn("Save the scene first", str(caught.exception))
            blend.write_bytes(b"BLENDER")
            with mock.patch.object(motion.pack, "_candidates", return_value=iter(())), \
                    mock.patch.dict(os.environ, {"LOOPCUT_BLENDER": ""}), self.assertRaises(service.LoopcutError) as caught:
                run.motion_apply(blend, bvh)
            self.assertIn("Blender not found", str(caught.exception))


class Tools(unittest.TestCase):
    def catalog(self, settings=None):
        tools = catalog.snapshot()["tools"]
        for tool in tools:
            if tool["name"].startswith("motion_"):
                tool["status"] = "live"
        return catalog.Catalog(tools, "server", account={"settings": settings or {}})

    def test_motion_tools_take_a_local_video_and_the_apply_args(self):
        tools = {tool["name"]: tool for tool in mcp_server.tool_list(self.catalog())}
        video = tools["motion_from_video"]["inputSchema"]
        self.assertEqual(video["required"], ["video"])
        self.assertIn("MP4 or MOV or WebM", video["properties"]["video"]["description"])
        for name in ("target_armature", "in_place", "start_s", "end_s", "static_camera", "confirm_token"):
            self.assertIn(name, video["properties"])
        self.assertIn("measures the clip itself", video["properties"]["video_seconds"]["description"])
        text = tools["motion_from_text"]
        self.assertIn("Meta Llama 3", text["description"])
        self.assertIn("Fingers stay still", text["description"])
        self.assertIn("target_armature", text["inputSchema"]["properties"])
        self.assertEqual(tools["motion_apply"]["inputSchema"]["required"], ["blend", "bvh"])
        names = list(tools)
        self.assertLess(names.index("motion_apply"), names.index("job_start"))
        self.assertNotIn("motion_apply", {t["name"] for t in mcp_server.tool_list(catalog.Catalog.from_snapshot())},
                         "motion_apply appears with the motion tools")

    def test_no_generation_keeps_capture_and_hides_text_to_motion(self):
        names = {tool["name"] for tool in mcp_server.tool_list(self.catalog({"no_generation": True}))}
        self.assertIn("motion_from_video", names)
        self.assertIn("motion_apply", names)
        self.assertNotIn("motion_from_text", names)

    def test_motion_apply_is_routed_with_its_arguments(self):
        calls = []

        class Fake:
            def motion_apply(self, blend, bvh, **options):
                calls.append((blend, bvh, options))
                return {"status": "applied"}

        self.assertEqual(mcp_server.call_tool(Fake(), "motion_apply", {"blend": "a.blend", "bvh": "m.bvh", "in_place": True,
                                                                        "target_armature": "Rig", "wait_seconds": 5}),
                         {"status": "applied"})
        self.assertEqual(calls[0][2]["armature"], "Rig")
        self.assertIs(calls[0][2]["in_place"], True)
        self.assertEqual(calls[0][2]["wait"], 5)
        with self.assertRaises(service.LoopcutError):
            mcp_server.call_tool(Fake(), "motion_apply", {"blend": "a.blend", "bvh": "m.bvh", "in_place": "yes"})


@unittest.skipUnless(MOCK.is_file(), "contract mock not available outside loopcut-web")
class MockMotion(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("loopcut_contract_mock_motion", MOCK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.server = module.create_server("127.0.0.1", 0, job_seconds=0.3,
                                           live_tools=["motion_from_video", "motion_from_text"])
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
        os.chdir(self.work)
        self.addCleanup(os.chdir, previous)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.service = service.Service(base_url=self.url, api_key="lc_test")

    def test_video_is_measured_uploaded_quoted_and_saved_with_apply_args_kept_local(self):
        clip = self.work / "kathak.mp4"
        clip.write_bytes(mp4(10.0, media_bytes=300_000))
        quote = self.service.quote_tool("motion_from_video", {"static_camera": True, "target_armature": "Hero",
                                                              "in_place": True}, {"video": str(clip)}, wait=20)
        self.assertEqual((quote["status"], quote["credits"]), ("quoted", 30), "3 credits a second of a 10 s clip")
        self.assertEqual(quote["input"], {"static_camera": True, "video_seconds": 10.0})
        self.assertEqual(quote["apply_args"], {"target_armature": "Hero", "in_place": True})
        sent = self.server.state.quotes[quote["quote_id"]]["body"]
        self.assertNotIn("target_armature", json.dumps(sent), "apply_args never reach the server")
        self.assertIn(hashlib.sha256(clip.read_bytes()).hexdigest(), self.server.state.blobs)
        result = self.service.start(quote["quote_id"], wait=20)
        self.assertEqual(result["status"], "succeeded")
        out = self.work / "loopcut_outputs" / result["job_id"]
        self.assertTrue((out / "motion.bvh").read_text(encoding="utf-8").startswith("HIERARCHY"))
        self.assertEqual(result["motion"]["source"], "gem-x")
        self.assertEqual(result["apply"]["motion_apply"], {"bvh": str(out / "motion.bvh"), "target_armature": "Hero",
                                                           "in_place": True})
        self.assertIn("title='kathak'", result["apply"]["bpy"])
        again = self.service.status(result["job_id"], wait=10, download=True)
        self.assertEqual(again["apply"], result["apply"], "a later job_status says the same")
        snippet = self.service.motion_snippet(out)
        self.assertIn("armature='Hero'", snippet)
        self.assertIn("in_place=True", snippet)

    def test_short_video_quotes_the_two_second_minimum(self):
        for seconds, credits in ((1.0, 6), (2.0, 6), (2.1, 9)):
            clip = self.work / f"short_{seconds}.mp4"
            clip.write_bytes(mp4(seconds, media_bytes=30_000))
            quote = self.service.quote_tool("motion_from_video", {}, {"video": str(clip)}, wait=20)
            self.assertEqual((quote["status"], quote["credits"]), ("quoted", credits), f"{seconds} s bills max(2, ceil) seconds")

    def test_text_to_motion_through_the_mcp_server_and_the_cli(self):
        result = mcp_server.call_tool(self.service, "motion_from_text", {"prompt": "a person waves", "duration_s": 4})
        self.assertEqual(result["status"], "quoted")
        job = mcp_server.call_tool(self.service, "job_start", {"quote_id": result["quote_id"], "wait_seconds": 20})
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["motion"]["fingers_animated"], False)
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URL": self.url, "LOOPCUT_API_KEY": "lc_test"}), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["motion-apply", "--bvh", job["apply"]["motion_apply"]["bvh"], "--bpy"]), 0)
        self.assertIn("title='a person waves'", stdout.getvalue())
        compile(stdout.getvalue(), "snippet", "exec")
        self.assertEqual(state.load("jobs", job["job_id"])["title"], "a person waves")

    def test_a_clip_over_a_minute_is_refused_before_uploading(self):
        clip = self.work / "long.mov"
        clip.write_bytes(mp4(75, brand=b"qt  "))
        with self.assertRaises(service.LoopcutError) as caught:
            self.service.quote_tool("motion_from_video", {}, {"video": str(clip)}, wait=5)
        self.assertEqual(caught.exception.code, "invalid_input")
        self.assertFalse(self.server.state.blobs, "nothing was uploaded")
        quote = self.service.quote_tool("motion_from_video", {"start_s": 10, "end_s": 40}, {"video": str(clip)}, wait=20)
        self.assertEqual(quote["credits"], 90)


if __name__ == "__main__":
    unittest.main()
