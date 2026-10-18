"""A stdio MCP server (JSON-RPC 2.0, one message per line) exposing Service as tools.

Only the tools capability is offered, with text results, which every MCP client since the
2024-11-05 protocol reads. Each tool call runs on its own thread so a long wait never blocks
pings or other calls; replies share one locked writer. Anything else that prints (the vendored
HTTP module logs transport failures) goes to stderr, because stdout carries only the protocol.

Tool calls return within MCP_WAIT_MAX seconds: Codex gives a tool 60 s by default. Long work
(packing, rendering, downloading) continues between calls, and each tool says what to call next.
"""

import json
import os
import sys
import threading
from pathlib import Path

from . import __version__
from .service import RenderError, Service

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL = "2025-06-18"
MCP_WAIT_MAX = 50
DEFAULT_WAIT = 40

INSTRUCTIONS = """\
Loopcut cloud render: renders a saved .blend on cloud GPUs (RTX 4090) and downloads the frames,
billed to the user's Loopcut account credits (1 credit = $0.01; 5 credits per GPU-minute, one-minute
minimum; quotes, packing and the QA report are free). Any Blender install works; Loopcut itself is
not needed. Flow: loopcut_login once -> render_quote (packs and uploads; call again while it says
"preparing") -> tell the user the price -> render_start -> render_status with wait_seconds until
succeeded -> render_fetch -> look at the frames. The cloud renders the file on disk: save the scene
first. Python in the .blend (drivers, startup scripts, add-ons) does not run in the cloud."""

_WAIT = {"type": "integer", "minimum": 0, "maximum": MCP_WAIT_MAX,
         "description": f"Seconds to wait for completion before returning progress (default {DEFAULT_WAIT})."}
_JOB = {"type": "string", "description": "Job id from render_start, e.g. job_…"}
_CONFIRM = {"type": "string", "description": "Only after a daily_limit_reached or rate_limited refusal that the user approved: the confirm_token it returned."}


# The server's render input schema (contract/fixtures/tool-descriptor/render.json), descriptions included.
RENDER_SETTINGS = json.loads(Path(__file__).with_name("render_input.json").read_text(encoding="utf-8"))["properties"]


TOOLS = [
    {"name": "loopcut_login",
     "description": ("Sign in to the user's Loopcut account (needed once per machine). Opens the sign-in page in the "
                     "browser and waits; if it returns a verification_url instead, show it to the user and call again "
                     "after they approve. Returns the email and plan once signed in."),
     "inputSchema": {"type": "object", "properties": {"wait_seconds": _WAIT}, "additionalProperties": False},
     "annotations": {"title": "Sign in to Loopcut", "readOnlyHint": False, "openWorldHint": True}},
    {"name": "loopcut_account",
     "description": "The signed-in Loopcut account: plan, credit balance and whether cloud render is available.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"title": "Loopcut account", "readOnlyHint": True, "openWorldHint": True}},
    {"name": "render_quote",
     "description": ("Price a cloud render of a saved .blend file. Collects the scene and every texture, library and "
                     "cache it uses with a local headless Blender, uploads what the cloud does not have yet, and "
                     "returns a quote_id with the price in credits. Spends nothing. Packing a big scene can take "
                     "minutes: while the result says status \"preparing\", call again with the same arguments. "
                     "Check warnings for missing files (they render pink). Frame settings default to frame 1 only; "
                     "pass frame_start/frame_end for an animation (the result shows the scene's own range)."),
     "inputSchema": {"type": "object",
                     "properties": {"blend": {"type": "string", "description": "Path to the saved .blend file."},
                                    **RENDER_SETTINGS,
                                    "blender": {"type": "string", "description": "Blender executable to pack with (default: LOOPCUT_BLENDER, PATH, then the usual install folders)."},
                                    "wait_seconds": _WAIT, "confirm_token": _CONFIRM},
                     "required": ["blend"], "additionalProperties": False},
     "annotations": {"title": "Quote a cloud render", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True}},
    {"name": "render_start",
     "description": ("Start the quoted render; this spends the user's Loopcut credits (actual GPU time, at most about "
                     "the quote). Only call it after the user has agreed to the quoted price or set a budget that "
                     "covers it. Calling it again for the same quote returns the same job instead of a second one."),
     "inputSchema": {"type": "object", "properties": {"quote_id": {"type": "string"}, "confirm_token": _CONFIRM},
                     "required": ["quote_id"], "additionalProperties": False},
     "annotations": {"title": "Start a cloud render (spends credits)", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True}},
    {"name": "render_status",
     "description": ("Status and progress of a render job. With wait_seconds it waits for the job to finish, so call "
                     "it repeatedly until status is succeeded, failed or canceled. A first render can spend several "
                     "minutes starting a GPU worker."),
     "inputSchema": {"type": "object", "properties": {"job_id": _JOB, "wait_seconds": _WAIT},
                     "required": ["job_id"], "additionalProperties": False},
     "annotations": {"title": "Render status", "readOnlyHint": True, "openWorldHint": True}},
    {"name": "render_fetch",
     "description": ("Download a finished render's PNG frames and QA report (default folder: loopcut_renders/<job_id> "
                     "next to the .blend). Returns frame paths and the QA summary with flagged frames. If files are "
                     "still pending when the time runs out, call again; finished files are kept."),
     "inputSchema": {"type": "object", "properties": {"job_id": _JOB,
                                                      "out_dir": {"type": "string", "description": "Folder to save into."},
                                                      "wait_seconds": _WAIT},
                     "required": ["job_id"], "additionalProperties": False},
     "annotations": {"title": "Download render frames", "readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": True}},
    {"name": "render_cancel",
     "description": "Cancel a queued or running render. Unspent credits are refunded.",
     "inputSchema": {"type": "object", "properties": {"job_id": _JOB}, "required": ["job_id"],
                     "additionalProperties": False},
     "annotations": {"title": "Cancel a render", "readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": True, "openWorldHint": True}},
]


def _wait(arguments):
    value = arguments.get("wait_seconds", DEFAULT_WAIT)
    if type(value) is not int or not 0 <= value <= MCP_WAIT_MAX:
        raise RenderError(f"wait_seconds must be an integer from 0 to {MCP_WAIT_MAX}", "invalid_input",
                          {"field": "wait_seconds"})
    return value


def _string(arguments, name):
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise RenderError(f"{name} is required", "invalid_input", {"field": name})
    return value


def call_tool(service, name, arguments):
    """Run one tool; returns a JSON-ready result or raises RenderError."""
    if not isinstance(arguments, dict):
        raise RenderError("Tool arguments must be an object", "invalid_input")
    if name == "loopcut_login":
        return service.login(wait=_wait(arguments))
    if name == "loopcut_account":
        return service.account()
    if name == "render_quote":
        settings = {key: arguments[key] for key in RENDER_SETTINGS if key in arguments}
        return service.quote(_string(arguments, "blend"), settings, blender=arguments.get("blender"),
                             wait=_wait(arguments), confirm_token=arguments.get("confirm_token"))
    if name == "render_start":
        return service.start(_string(arguments, "quote_id"), confirm_token=arguments.get("confirm_token"))
    if name == "render_status":
        return service.status(_string(arguments, "job_id"), wait=_wait(arguments))
    if name == "render_fetch":
        return service.fetch(_string(arguments, "job_id"), arguments.get("out_dir"), wait=_wait(arguments))
    if name == "render_cancel":
        return service.cancel(_string(arguments, "job_id"))
    raise RenderError(f"Unknown tool: {name}", "unknown_tool")


class Server:
    def __init__(self, service, output):
        self.service = service
        self._output = output
        self._write_lock = threading.Lock()
        self._calls = []

    def _send(self, message):
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            self._output.write(line.encode("utf-8"))
            self._output.flush()

    def _reply(self, identifier, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": identifier}
        message.update({"error": error} if error else {"result": result})
        self._send(message)

    def _tool_reply(self, identifier, params):
        try:
            result, is_error = call_tool(self.service, params.get("name"), params.get("arguments") or {}), False
        except RenderError as ex:
            result, is_error = ex.as_dict(), True
        except Exception as ex:  # A bug must not kill the server; the agent sees the failure.
            print(f"loopcut-render: {params.get('name')} failed: {ex!r}", file=sys.stderr)
            result, is_error = {"error": {"code": "internal_error", "message": f"{type(ex).__name__}: {ex}"}}, True
        self._reply(identifier, {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}],
                                 "isError": is_error})

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "id" in message and "method" not in message:
                return  # A response to a server request; this server sends none.
            self._reply(message.get("id") if isinstance(message, dict) else None,
                        error={"code": -32600, "message": "Invalid request"})
            return
        method, identifier, params = message["method"], message.get("id"), message.get("params") or {}
        if "id" not in message:
            return  # Notifications (initialized, cancelled, …) need no reply.
        if method == "initialize":
            requested = params.get("protocolVersion")
            self._reply(identifier, {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else FALLBACK_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "loopcut-render", "title": "Loopcut cloud render", "version": __version__},
                "instructions": INSTRUCTIONS})
        elif method == "ping":
            self._reply(identifier, {})
        elif method == "tools/list":
            self._reply(identifier, {"tools": TOOLS})
        elif method == "tools/call":
            call = threading.Thread(target=self._tool_reply, args=(identifier, params), daemon=True)
            self._calls = [thread for thread in self._calls if thread.is_alive()] + [call]
            call.start()
        else:
            self._reply(identifier, error={"code": -32601, "message": f"Method not found: {method}"})

    def serve(self, lines):
        for raw in lines:
            if not raw.strip():
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                self._reply(None, error={"code": -32700, "message": "Parse error"})
                continue
            for item in message if isinstance(message, list) else [message]:
                self.handle(item)
        for call in self._calls:  # Input closed: answer the calls already running, then exit.
            call.join()


def main(service=None):
    # Only protocol messages may reach the client's pipe. Keep it on a private descriptor and point
    # fd 1 at stderr, so stray prints and child processes (the browser opener, Blender) cannot write to it.
    sys.stdout.flush()
    output = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    Server(service or Service(), output).serve(sys.stdin.buffer)
    return 0
