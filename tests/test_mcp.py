"""The stdio MCP server: handshake, tool list and tool calls over real pipes."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from loopcut_render import mcp_server
from loopcut_render.service import RenderError

SRC = Path(__file__).resolve().parents[1] / "src"


class FakeService:
    def __init__(self):
        self.calls = []

    def status(self, job_id, wait=0):
        self.calls.append(("status", job_id, wait))
        return {"job_id": job_id, "status": "running"}

    def start(self, quote_id, confirm_token=None):
        raise RenderError("Not enough credits.", "insufficient_credits", {"balance": 3, "needed": 5})


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
        self.assertIn("tools", replies[1]["result"]["capabilities"])
        self.assertIn("render_quote", replies[1]["result"]["instructions"])

    def test_tools_list_schemas(self):
        tools = {tool["name"]: tool for tool in self.serve(rpc(1, "tools/list"))[1]["result"]["tools"]}
        self.assertEqual(set(tools), {"loopcut_login", "loopcut_account", "render_quote", "render_start",
                                      "render_status", "render_fetch", "render_cancel"})
        quote = tools["render_quote"]["inputSchema"]
        self.assertEqual(quote["required"], ["blend"])
        for name in ("frame_start", "frame_end", "samples", "engine", "turntable", "wait_seconds"):
            self.assertIn(name, quote["properties"])

    def test_tool_call_success_and_error(self):
        service = FakeService()
        replies = self.serve(rpc(1, "tools/call", {"name": "render_status", "arguments": {"job_id": "job_1", "wait_seconds": 7}}),
                             rpc(2, "tools/call", {"name": "render_start", "arguments": {"quote_id": "qt_1"}}),
                             rpc(3, "tools/call", {"name": "render_status", "arguments": {"job_id": "job_1", "wait_seconds": 99}}),
                             service=service)
        self.assertFalse(replies[1]["result"]["isError"])
        self.assertEqual(json.loads(replies[1]["result"]["content"][0]["text"])["status"], "running")
        self.assertEqual(service.calls, [("status", "job_1", 7)])
        error = json.loads(replies[2]["result"]["content"][0]["text"])["error"]
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertEqual((error["code"], error["needed"]), ("insufficient_credits", 5))
        self.assertTrue(replies[3]["result"]["isError"], "wait_seconds over the cap is refused")

    def test_notifications_unknown_methods_and_bad_json(self):
        output = Collect()
        server = mcp_server.Server(FakeService(), output)
        server.serve([b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n', b"not json\n",
                      rpc(5, "resources/list").encode(), rpc(6, "ping").encode()])
        replies = output.messages()
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["error"]["code"], -32601)
        self.assertEqual(replies[2], {"jsonrpc": "2.0", "id": 6, "result": {}})

    def test_stdio_process_keeps_stdout_clean(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "PYTHONPATH": str(SRC), "LOOPCUT_RENDER_HOME": home, "LOOPCUT_API_KEY": "",
                   "LOOPCUT_SERVICE_URL": "http://127.0.0.1:9"}
            process = subprocess.run([sys.executable, "-m", "loopcut_render", "mcp"], env=env, capture_output=True, timeout=30,
                                     input=(rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
                                            + '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                                            + rpc(2, "tools/list")).encode())
        replies = [json.loads(line) for line in process.stdout.decode().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])

    def test_child_processes_cannot_write_to_the_protocol_pipe(self):
        script = (
            "import os, subprocess\n"
            "from loopcut_render import mcp_server\n"
            "class Noisy:\n"
            "    def status(self, job_id, wait=0):\n"
            "        print('a stray print')\n"
            "        subprocess.run(['echo', 'a child process'])\n"
            "        return {'job_id': job_id, 'status': 'running'}\n"
            "mcp_server.main(Noisy())\n")
        process = subprocess.run([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": str(SRC)},
                                 input=rpc(1, "tools/call", {"name": "render_status", "arguments": {"job_id": "job_1", "wait_seconds": 0}}).encode(),
                                 capture_output=True, timeout=30)
        lines = process.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertFalse(json.loads(lines[0])["result"]["isError"])
        self.assertIn(b"a child process", process.stderr)


if __name__ == "__main__":
    unittest.main()
