"""The stdio MCP server: handshake, the catalog-driven tool list and tool calls over real pipes."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from loopcut import catalog, mcp_server
from loopcut.service import LoopcutError

SRC = Path(__file__).resolve().parents[1] / "src"
LIVE = {"loopcut_login", "loopcut_account", "render_quote", "asset_search", "image_to_3d", "remesh", "sound_effect",
        "voice_line", "job_start", "job_status", "job_cancel"}


class FakeService:
    def __init__(self, known=None):
        self.calls = []
        self.known = known or catalog.Catalog.from_snapshot()
        self.on_catalog_change = None
        self.roots = []

    def catalog(self, refresh=False):
        return self.known

    def status(self, job_id, wait=0, download=False, out_dir=None):
        self.calls.append(("status", job_id, wait, download))
        return {"job_id": job_id, "status": "running"}

    def start(self, quote_id, confirm_token=None, wait=0, out_dir=None):
        raise LoopcutError("Not enough credits.", "insufficient_credits", {"balance": 3, "needed": 5})

    def quote_tool(self, name, inputs, files, confirm_token=None, wait=None):
        self.calls.append(("quote_tool", name, inputs, files, confirm_token))
        return {"status": "quoted", "quote_id": "qt_1", "credits": 4}


class Collect:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    def flush(self):
        pass

    def messages(self):
        return [json.loads(line) for line in self.data.decode().splitlines()]


def rpc(identifier, method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}) + "\n"


def call(identifier, name, arguments):
    return rpc(identifier, "tools/call", {"name": name, "arguments": arguments})


def text(reply):
    return json.loads(reply["result"]["content"][0]["text"])


class Protocol(unittest.TestCase):
    def serve(self, *lines, service=None):
        output = Collect()
        server = mcp_server.Server(service or FakeService(), output)
        server.serve([line.encode() for line in lines])
        deadline = time.monotonic() + 5
        expected = sum(1 for line in lines if '"id"' in line)
        while len(output.messages()) < expected and time.monotonic() < deadline:
            time.sleep(0.01)  # Tool calls answer from their own threads.
        return {m.get("id"): m for m in output.messages()}

    def test_initialize_negotiates_version(self):
        replies = self.serve(rpc(1, "initialize", {"protocolVersion": "2025-03-26"}),
                             rpc(2, "initialize", {"protocolVersion": "2099-01-01"}))
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[2]["result"]["protocolVersion"], mcp_server.FALLBACK_PROTOCOL)
        self.assertEqual(replies[1]["result"]["capabilities"]["tools"], {"listChanged": True})
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "loopcut")
        self.assertIn("job_start", replies[1]["result"]["instructions"])

    def test_tools_list_follows_the_catalog(self):
        tools = {tool["name"]: tool for tool in self.serve(rpc(1, "tools/list"))[1]["result"]["tools"]}
        self.assertEqual(set(tools), LIVE, "coming_soon tools stay hidden")
        self.assertLessEqual(len(tools), 12)
        render = tools["render_quote"]["inputSchema"]
        self.assertEqual(render["required"], ["blend"])
        for name in ("frame_start", "frame_end", "samples", "engine", "turntable", "wait_seconds", "confirm_token"):
            self.assertIn(name, render["properties"])
        self.assertEqual(tools["remesh"]["inputSchema"]["required"], ["model"], "required file inputs are required")
        self.assertIn("GLB", tools["remesh"]["inputSchema"]["properties"]["model"]["description"])
        self.assertIn("image", tools["image_to_3d"]["inputSchema"]["properties"])
        self.assertIn("Free", tools["asset_search"]["description"])
        self.assertIn("job_start", tools["sound_effect"]["description"])
        for tool in tools.values():
            self.assertLess(len(tool["description"]), 520, tool["name"])

    def test_every_tool_is_ready_for_the_directories(self):
        value = catalog.snapshot()
        for tool in value["tools"]:
            tool["status"] = "live"  # The coming_soon tools too, as they will look once open.
        for tool in mcp_server.tool_list(catalog.Catalog(value["tools"], "server")):
            with self.subTest(tool["name"]):
                self.assertRegex(tool["name"], r"^[A-Za-z0-9_-]{1,64}$")
                self.assertTrue(tool["title"])
                annotations = tool["annotations"]
                self.assertEqual(annotations["title"], tool["title"])
                self.assertTrue(isinstance(annotations.get("readOnlyHint"), bool)
                                or isinstance(annotations.get("destructiveHint"), bool))
                self.assertIs(annotations["openWorldHint"], True)
                if annotations.get("readOnlyHint") is False:
                    self.assertIsInstance(annotations.get("destructiveHint"), bool)
        tools = {t["name"]: t["annotations"] for t in mcp_server.tool_list(catalog.Catalog.from_snapshot())}
        for name in ("render_quote", "asset_search", "sound_effect", "job_status", "loopcut_account"):
            self.assertIs(tools[name]["readOnlyHint"], True, name)
        for name in ("job_start", "job_cancel"):
            self.assertIs(tools[name]["destructiveHint"], True, name)

    def test_a_catalog_name_over_64_characters_is_not_offered(self):
        value = catalog.snapshot()
        long = dict(next(t for t in value["tools"] if t["name"] == "lip_sync"), name="x" * 65, status="live")
        names = {t["name"] for t in mcp_server.tool_list(catalog.Catalog([*value["tools"], long], "server"))}
        self.assertNotIn("x" * 65, names)

    def test_results_default_to_the_clients_workspace_root(self):
        service = FakeService()
        output = Collect()
        server = mcp_server.Server(service, output)
        with tempfile.TemporaryDirectory() as folder:
            uri = Path(folder).resolve().as_uri()
            server.serve([rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {"roots": {}}}).encode(),
                          b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'])
            request = output.messages()[-1]
            self.assertEqual(request["method"], "roots/list")
            server.serve([json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                      "result": {"roots": [{"uri": uri, "name": "project"}]}}).encode()])
            self.assertEqual(service.roots, [Path(folder).resolve()])
        quiet = Collect()
        mcp_server.Server(FakeService(), quiet).serve([rpc(1, "initialize", {"protocolVersion": "2025-06-18"}).encode(),
                                                       b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'])
        self.assertEqual(len(quiet.messages()), 1, "no roots request to a client without roots")

    def test_a_tool_opening_on_the_server_appears_and_is_announced(self):
        service = FakeService()
        output = Collect()
        server = mcp_server.Server(service, output)
        server.serve([rpc(1, "tools/list").encode()])
        value = catalog.snapshot()
        for tool in value["tools"]:
            if tool["name"] in {"image_generate", "image_edit"}:
                tool["status"] = "live"
        service.known = catalog.Catalog(value["tools"], "server")
        service.on_catalog_change(service.known)
        service.on_catalog_change(service.known)  # Unchanged: announced once.
        messages = output.messages()
        self.assertEqual([m.get("method") for m in messages[1:]], ["notifications/tools/list_changed"])
        server.serve([rpc(2, "tools/list").encode()])
        names = {tool["name"] for tool in output.messages()[-1]["result"]["tools"]}
        self.assertEqual(names, LIVE | {"image_generate", "image_edit"})
        edit = next(t for t in output.messages()[-1]["result"]["tools"] if t["name"] == "image_edit")
        self.assertEqual(sorted(edit["inputSchema"]["required"]), ["image", "prompt"])

    def test_no_generation_hides_generative_tools_and_private_mode_is_explained(self):
        value = catalog.snapshot()
        for tool in value["tools"]:
            if tool["name"] == "sfx":
                tool.update(available=False, unavailable_reason="private_mode")
        known = catalog.Catalog(value["tools"], "server", account={"settings": {"no_generation": False, "private_mode": True}})
        tools = {t["name"]: t for t in mcp_server.tool_list(known)}
        self.assertTrue(tools["sound_effect"]["description"].startswith("Blocked on this account while Private mode"))
        known = catalog.Catalog(value["tools"], "server", account={"settings": {"no_generation": True}})
        names = {t["name"] for t in mcp_server.tool_list(known)}
        self.assertEqual(names, LIVE - {"image_to_3d", "sound_effect", "voice_line"})

    def test_tool_calls_route_inputs_files_and_errors(self):
        service = FakeService()
        replies = self.serve(call(1, "job_status", {"job_id": "job_1", "wait_seconds": 7}),
                             call(2, "job_start", {"quote_id": "qt_1"}),
                             call(3, "job_status", {"job_id": "job_1", "wait_seconds": 99}),
                             call(4, "image_to_3d", {"prompt": "a lamp", "image": "/tmp/lamp.png"}),
                             call(5, "sound_effect", {"prompt": "door slam", "confirm_token": "ct_1"}),
                             call(6, "image_generate", {"prompt": "x"}),
                             service=service)
        self.assertFalse(replies[1]["result"]["isError"])
        self.assertEqual(text(replies[1])["status"], "running")
        self.assertIn(("status", "job_1", 7, True), service.calls, "job_status downloads finished results")
        error = text(replies[2])["error"]
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertEqual((error["code"], error["needed"]), ("insufficient_credits", 5))
        self.assertTrue(replies[3]["result"]["isError"], "wait_seconds over the cap is refused")
        self.assertIn(("quote_tool", "image_to_3d", {"prompt": "a lamp"}, {"image": "/tmp/lamp.png"}, None), service.calls)
        self.assertIn(("quote_tool", "sfx", {"prompt": "door slam"}, {}, "ct_1"), service.calls)
        self.assertEqual(text(replies[6])["error"]["code"], "unknown_tool", "a coming_soon tool can't be called")

    def test_notifications_unknown_methods_and_bad_json(self):
        output = Collect()
        server = mcp_server.Server(FakeService(), output)
        server.serve([b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n', b"not json\n",
                      rpc(5, "resources/list").encode(), rpc(6, "ping").encode()])
        replies = output.messages()
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["error"]["code"], -32601)
        self.assertEqual(replies[2], {"jsonrpc": "2.0", "id": 6, "result": {}})

    def test_stdio_process_keeps_stdout_clean_and_falls_back_to_the_snapshot(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "PYTHONPATH": str(SRC), "LOOPCUT_CONNECTOR_HOME": home, "LOOPCUT_API_KEY": "",
                   "LOOPCUT_SERVICE_URL": "http://127.0.0.1:9"}
            process = subprocess.run([sys.executable, "-m", "loopcut", "mcp"], env=env, capture_output=True, timeout=30,
                                     input=(rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
                                            + '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                                            + rpc(2, "tools/list")).encode())
        replies = [json.loads(line) for line in process.stdout.decode().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual({tool["name"] for tool in replies[1]["result"]["tools"]}, LIVE)

    def test_child_processes_cannot_write_to_the_protocol_pipe(self):
        script = (
            "import os, subprocess\n"
            "from loopcut import mcp_server\n"
            "class Noisy:\n"
            "    def status(self, job_id, wait=0, download=False, out_dir=None):\n"
            "        print('a stray print')\n"
            "        subprocess.run(['echo', 'a child process'])\n"
            "        return {'job_id': job_id, 'status': 'running'}\n"
            "mcp_server.main(Noisy())\n")
        process = subprocess.run([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": str(SRC)},
                                 input=call(1, "job_status", {"job_id": "job_1", "wait_seconds": 0}).encode(),
                                 capture_output=True, timeout=30)
        lines = process.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertFalse(json.loads(lines[0])["result"]["isError"])
        self.assertIn(b"a child process", process.stderr)


class Descriptions(unittest.TestCase):
    def test_a_tool_newer_than_the_client_uses_the_servers_words(self):
        descriptor = copy.deepcopy(next(t for t in catalog.snapshot()["tools"] if t["name"] == "lip_sync"))
        descriptor.update(name="mouth_shapes", status="live")
        tool = mcp_server.cloud_tool(descriptor)
        self.assertEqual(tool["name"], "mouth_shapes")
        self.assertNotIn("Applying the result", tool["description"])
        self.assertTrue(tool["description"].endswith(mcp_server._PAID.strip()))


if __name__ == "__main__":
    unittest.main()
