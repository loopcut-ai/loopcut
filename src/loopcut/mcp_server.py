"""A stdio MCP server (JSON-RPC 2.0, one message per line) exposing Service as tools.

The tool list follows the server's catalog: one quote tool per open Loopcut tool, whose input
schema is the catalog's, plus sign-in, account and the shared job tools. When the catalog changes
(a tool opens, the user signs in, an account setting changes) the server sends
notifications/tools/list_changed. Only the tools capability is offered, with text results, which
every MCP client since the 2024-11-05 protocol reads. Each tool call runs on its own thread so a
long wait never blocks pings or other calls; replies share one locked writer. Anything else that
prints (the vendored HTTP module logs transport failures) goes to stderr, because stdout carries
only the protocol.

Tool calls return within MCP_WAIT_MAX seconds: Codex gives a tool 60 s by default. Long work
(packing, uploading, rendering, downloading) continues between calls, and each result says what
to call next.
"""

import copy
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from . import catalog as catalogs
from .service import RENDER, LoopcutError, Service

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL = "2025-06-18"
MCP_WAIT_MAX = 50
DEFAULT_WAIT = 40

INSTRUCTIONS = """\
Loopcut cloud tools for Blender work, paid from the user's Loopcut credits (1 credit = $0.01).
The first call opens a browser to sign in (or returns a verification_url to show the user;
then call the same tool again). Every paid tool returns a quote first and spends nothing: tell the
user the price and call job_start only after they agree, or when it is within a budget they gave.
asset_search is free and runs at once: try it before generating a model or texture. For a long
render, quote a small test frame first. job_start and job_status wait for the job and download the
results: renders into loopcut_renders/<job_id>/ next to the .blend, everything else into
loopcut_outputs/<job_id>/ in the working folder. Import them with bpy (GLB or FBX models, WAV audio,
PNG images). Save the scene before render_quote; the cloud renders the file on disk. Motion results
(motion.bvh) go onto the user's rig with motion_apply, which is free and runs a local Blender."""

_WAIT = {"type": "integer", "minimum": 0, "maximum": MCP_WAIT_MAX,
         "description": f"Seconds to wait before returning progress (default {DEFAULT_WAIT})."}
_JOB = {"type": "string", "description": "Job id from job_start, e.g. job_…"}
_OUT = {"type": "string", "description": "Folder for the results (default: loopcut_outputs/<job_id>, or loopcut_renders/<job_id> next to the .blend)."}
_CONFIRM = {"type": "string", "description": "Only if the user approved going past a daily_limit_reached or rate_limited refusal: its confirm_token."}
_PAID = " Returns a quote and spends nothing; call job_start once the user agrees to the price."
_FREE = " Free: runs at once and returns the results."
_BLOCKED = {"private_mode": "Blocked on this account while Private mode is on (its provider may train on inputs). ",
            "plan": "Not included in this account's plan. "}


def _fixed(name, title, description, properties, required=(), **hints):
    # Directory listings need a title and readOnlyHint or destructiveHint on every tool.
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False},
            "annotations": {"title": title, "openWorldHint": True, **hints}}


LOGIN = _fixed("loopcut_login", "Sign in to Loopcut", (
    "Sign in to the user's Loopcut account, or switch accounts. Any tool called while signed out does this by itself. "
    "Opens the sign-in page in the browser and waits; if it returns a verification_url instead, show it to the user "
    "and call again after they approve."),
    {"wait_seconds": _WAIT}, readOnlyHint=False, destructiveHint=False, idempotentHint=True)
ACCOUNT = _fixed("loopcut_account", "Loopcut account", (
    "The signed-in Loopcut account: plan, credit balance, Private mode and no-generation settings, and which tools "
    "it can use."), {}, readOnlyHint=True)
JOB_START = _fixed("job_start", "Start a quoted job (spends credits)", (
    "Start a quoted job; this spends the user's Loopcut credits, at most the quote. Only call it after the user agreed "
    "to the quoted price or set a budget that covers it. Calling it again for the same quote returns the same job. "
    "Waits for the job and downloads the results when it finishes in time."),
    {"quote_id": {"type": "string"}, "wait_seconds": _WAIT, "out_dir": _OUT, "confirm_token": _CONFIRM}, ["quote_id"],
    readOnlyHint=False, destructiveHint=True, idempotentHint=True)  # Spending credits can't be undone.
JOB_STATUS = _fixed("job_status", "Job status and results", (
    "Wait for a job, then download its results (hash-checked; finished files are kept). Call it again until the "
    "result has the files or the status is failed or canceled. A first render can spend minutes starting a GPU."),
    {"job_id": _JOB, "wait_seconds": _WAIT, "out_dir": _OUT}, ["job_id"],
    readOnlyHint=True)  # Changes nothing on the account; it only saves the job's results locally.
JOB_CANCEL = _fixed("job_cancel", "Cancel a job", "Cancel a queued or running job. Unspent credits are refunded.",
                    {"job_id": _JOB}, ["job_id"], readOnlyHint=False, destructiveHint=True, idempotentHint=True)
MOTION_APPLY = _fixed("motion_apply", "Put a motion on a rig", (
    "Retarget a motion_from_video or motion_from_text result (motion.bvh) onto the humanoid rig in a saved .blend "
    "(Mixamo, Rigify, Auto-Rig Pro, Unreal mannequin or any humanoid by bone names) with a local headless Blender, "
    "then save the file; the previous version stays as .blend1. Free, nothing leaves the machine. target_armature "
    "and in_place default to what the motion tool was called with; without an armature it uses the active, the "
    "selected or the only humanoid armature, and with none it adds SOMA's skeleton with a simple body. If the "
    ".blend is open in Blender, run the job result's apply.bpy in that session instead. While the result says "
    "status \"applying\", call again with the same arguments."),
    {"blend": {"type": "string", "description": "Path to the saved .blend to animate."},
     "bvh": {"type": "string", "description": "Path to motion.bvh from job_status (or its folder)."},
     "target_armature": {"type": "string", "description": "Name of the armature object (or a mesh it deforms) to animate."},
     "in_place": {"type": "boolean", "description": "Keep the pelvis over its starting position: no root travel."},
     "frame_start": {"type": "integer", "description": "First frame of the motion (default: the scene's start frame)."},
     "out": {"type": "string", "description": "Save to this .blend instead of overwriting the input."},
     "blender": {"type": "string", "description": "Blender executable (default: LOOPCUT_BLENDER, PATH, then the usual install folders)."},
     "wait_seconds": _WAIT},
    ["blend", "bvh"], readOnlyHint=False, destructiveHint=True, idempotentHint=False)


def _description(descriptor):
    entry = catalogs.AGENT_TOOLS.get(descriptor["name"])
    if entry:
        text = entry["description"]
    else:  # A tool newer than this client: the server's own words, without the add-on's apply step.
        text = ". ".join(sentence for sentence in descriptor.get("description", "").split(". ")
                         if "Applying the result" not in sentence).strip().rstrip(".") + "."
    free = (descriptor.get("cost") or {}).get("credits_estimate") == 0
    blocked = _BLOCKED.get(descriptor.get("unavailable_reason")) if descriptor.get("available") is False else None
    if descriptor["name"] == RENDER:
        return (blocked or "") + text + _PAID.replace("job_start", "job_start with the quote_id")
    return (blocked or "") + text + (_FREE if free else _PAID)


def _file_property(spec):
    kinds = " or ".join(catalogs.MIME_NAMES.get(mime, mime) for mime in spec["accept"])
    return {"type": "string", "description": f"Local path to a {kinds} file, at most {spec['max_mb']} MB."}


def _lean(schema, names=False):
    """The schema without regular expressions: the server checks them, and they only cost the agent context.
    names: schema is a properties map, whose keys are input names, not keywords."""
    if isinstance(schema, dict):
        return {key: _lean(value, key == "properties" and not names) for key, value in schema.items()
                if names or key != "pattern"}
    if isinstance(schema, list):
        return [_lean(value) for value in schema]
    return schema


def cloud_tool(descriptor):
    """The MCP tool for one catalog tool: its input schema, its file inputs as local paths, and a confirm token."""
    schema = _lean(copy.deepcopy(descriptor["input_schema"]))
    properties = dict(schema.get("properties") or {})
    required = list(schema.get("required") or [])
    if descriptor["name"] == RENDER:
        properties = {"blend": {"type": "string", "description": "Path to the saved .blend file."}, **properties,
                      "blender": {"type": "string", "description": "Blender executable to pack with (default: LOOPCUT_BLENDER, PATH, then the usual install folders)."},
                      "wait_seconds": _WAIT}
        required = ["blend", *required]
    for spec in descriptor.get("file_inputs") or []:
        properties[spec["field"]] = _file_property(spec)
        if spec.get("required"):
            required.append(spec["field"])
    if descriptor.get("apply") == "apply_motion":
        # Client-only (contract 0.7): kept for motion_apply, never sent to the server.
        for name, spec in ((descriptor.get("apply_args") or {}).get("properties") or {}).items():
            properties.setdefault(name, _lean(copy.deepcopy(spec)))
        if "video_seconds" in properties:
            properties["video_seconds"] = {**properties["video_seconds"], "description": (
                "Leave it out: the connector measures the clip itself. Only when it asks (a WebM without ffprobe), "
                "the clip's length in seconds.")}
    free = (descriptor.get("cost") or {}).get("credits_estimate") == 0
    if not free:  # Free tools are never held by a spending limit.
        properties["confirm_token"] = _CONFIRM
    display = descriptor.get("display") or {}
    title = display.get("title") or descriptor.get("title") or descriptor["name"]
    # Quoting (and the free asset search) spends nothing and changes nothing on the account.
    return {"name": catalogs.agent_name(descriptor["name"]), "title": title, "description": _description(descriptor),
            "inputSchema": {"type": "object", "properties": properties, "required": list(dict.fromkeys(required)),
                            "additionalProperties": False},
            "annotations": {"title": title, "readOnlyHint": True, "openWorldHint": True}}


def tool_list(catalog):
    visible = catalog.visible()
    local = [MOTION_APPLY] if any(descriptor.get("apply") == "apply_motion" for descriptor in visible) else []
    return [LOGIN, ACCOUNT, *(cloud_tool(descriptor) for descriptor in visible), *local, JOB_START, JOB_STATUS, JOB_CANCEL]


def _wait(arguments, cap=MCP_WAIT_MAX):
    value = arguments.get("wait_seconds", DEFAULT_WAIT)
    if type(value) is not int or not 0 <= value <= MCP_WAIT_MAX:
        raise LoopcutError(f"wait_seconds must be an integer from 0 to {MCP_WAIT_MAX}", "invalid_input",
                           {"field": "wait_seconds"})
    return max(0, min(value, cap))


def _string(arguments, name):
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise LoopcutError(f"{name} is required", "invalid_input", {"field": name})
    return value


SIGNED_OUT = {"sign_in_required", "invalid_api_key"}
SIGN_IN_MARGIN = 3  # Seconds kept back from the sign-in wait for the original call's own requests.


def call_tool(service, name, arguments, budget=MCP_WAIT_MAX):
    """Run one tool; returns a JSON-ready result or raises LoopcutError.

    Signed out (no stored key, or a revoked one, and no LOOPCUT_API_KEY), any tool that needs the
    account starts the browser sign-in, as loopcut_login does, and waits for approval for what is
    left of the time budget. Approved in time, the original call goes ahead; otherwise the result is
    the sign-in link, and the same pending sign-in is picked up when the agent calls again."""
    started = time.monotonic()
    try:
        return _dispatch(service, name, arguments, budget)
    except LoopcutError as ex:
        if ex.code not in SIGNED_OUT or name == "loopcut_login" or not getattr(service, "can_sign_in", lambda: False)():
            raise
    left = lambda: budget - (time.monotonic() - started)  # noqa: E731
    signed = service.login(wait=max(0, int(left() - SIGN_IN_MARGIN)))
    if not signed.get("signed_in"):
        return {**signed, "next": (f"Show the user verification_url to sign in to Loopcut (code {signed.get('user_code')}); "
                                   f"once they approve, call {name} again with the same arguments.")}
    return _dispatch(service, name, arguments, max(0, int(left())))


def _dispatch(service, name, arguments, cap):
    if not isinstance(arguments, dict):
        raise LoopcutError("Tool arguments must be an object", "invalid_input")
    if name == "loopcut_login":
        return service.login(wait=_wait(arguments, cap))
    if name == "loopcut_account":
        return service.account()
    if name == "job_start":
        return service.start(_string(arguments, "quote_id"), confirm_token=arguments.get("confirm_token"),
                             wait=_wait(arguments, cap), out_dir=arguments.get("out_dir"))
    if name == "job_status":
        return service.status(_string(arguments, "job_id"), wait=_wait(arguments, cap), download=True,
                              out_dir=arguments.get("out_dir"))
    if name == "job_cancel":
        return service.cancel(_string(arguments, "job_id"))
    if name == "motion_apply":
        in_place = arguments.get("in_place")
        if in_place is not None and type(in_place) is not bool:
            raise LoopcutError("in_place must be true or false", "invalid_input", {"field": "in_place"})
        return service.motion_apply(_string(arguments, "blend"), _string(arguments, "bvh"),
                                    armature=arguments.get("target_armature") or None, in_place=in_place,
                                    frame_start=arguments.get("frame_start"), out=arguments.get("out") or None,
                                    blender=arguments.get("blender") or None, wait=_wait(arguments, cap))
    descriptor = service.catalog().find(name) if isinstance(name, str) else None
    if descriptor is None:
        raise LoopcutError(f"Unknown tool: {name}. The tool list may have changed; list the tools again.", "unknown_tool")
    if descriptor["name"] == RENDER:
        settings = {key: arguments[key] for key in descriptor["input_schema"].get("properties", {}) if key in arguments}
        return service.quote(_string(arguments, "blend"), settings, blender=arguments.get("blender"),
                             wait=_wait(arguments, cap), confirm_token=arguments.get("confirm_token"))
    fields = {spec["field"] for spec in descriptor.get("file_inputs") or []}
    files = {key: value for key, value in arguments.items() if key in fields}
    inputs = {key: value for key, value in arguments.items() if key not in fields and key != "confirm_token"}
    return service.quote_tool(descriptor["name"], inputs, files, confirm_token=arguments.get("confirm_token"),
                              wait=min(DEFAULT_WAIT, cap))


class Server:
    def __init__(self, service, output):
        self.service = service
        self._output = output
        self._write_lock = threading.Lock()
        self._calls = []
        self._listed = None  # The tool list the client last saw.
        self._roots = False  # Whether the client offers roots (its workspace folders).
        self._requests = 0
        if hasattr(service, "on_catalog_change"):
            service.on_catalog_change = self._catalog_changed

    def _send(self, message):
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            self._output.write(line.encode("utf-8"))
            self._output.flush()

    def _reply(self, identifier, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": identifier}
        message.update({"error": error} if error else {"result": result})
        self._send(message)

    def _catalog_changed(self, catalog):
        if self._listed is None:
            return
        tools = tool_list(catalog)
        if tools != self._listed:
            self._listed = tools
            self._send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def _tools(self):
        tools = tool_list(self.service.catalog())
        self._listed = tools
        return tools

    def _tool_reply(self, identifier, params):
        try:
            result, is_error = call_tool(self.service, params.get("name"), params.get("arguments") or {}), False
        except LoopcutError as ex:
            result, is_error = ex.as_dict(), True
        except Exception as ex:  # A bug must not kill the server; the agent sees the failure.
            print(f"loopcut: {params.get('name')} failed: {ex!r}", file=sys.stderr)
            result, is_error = {"error": {"code": "internal_error", "message": f"{type(ex).__name__}: {ex}"}}, True
        self._reply(identifier, {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}],
                                 "isError": is_error})

    def _ask_roots(self):
        """Ask the client for its workspace folders; results then default to the first one."""
        self._requests += 1
        self._send({"jsonrpc": "2.0", "id": f"loopcut-roots-{self._requests}", "method": "roots/list"})

    def _answer(self, message):
        """A response to one of this server's requests: only roots/list is ever sent."""
        roots = (message.get("result") or {}).get("roots") if str(message.get("id", "")).startswith("loopcut-roots-") else None
        if not isinstance(roots, list) or not hasattr(self.service, "roots"):
            return
        folders = []
        for root in roots:
            uri = root.get("uri") if isinstance(root, dict) else None
            if isinstance(uri, str) and uri.startswith("file://"):
                folders.append(Path(urllib.request.url2pathname(urllib.parse.urlsplit(uri).path)))
        self.service.roots = folders

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            if isinstance(message, dict) and "id" in message and "method" not in message:
                self._answer(message)
                return
            self._reply(message.get("id") if isinstance(message, dict) else None,
                        error={"code": -32600, "message": "Invalid request"})
            return
        method, identifier, params = message["method"], message.get("id"), message.get("params") or {}
        if "id" not in message:
            if self._roots and method in {"notifications/initialized", "notifications/roots/list_changed"}:
                self._ask_roots()
            return  # Other notifications (cancelled, …) need no reply.
        if method == "initialize":
            self._roots = isinstance((params.get("capabilities") or {}).get("roots"), dict)
            requested = params.get("protocolVersion")
            self._reply(identifier, {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else FALLBACK_PROTOCOL,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "loopcut", "title": "Loopcut", "version": __version__},
                "instructions": INSTRUCTIONS})
        elif method == "ping":
            self._reply(identifier, {})
        elif method == "tools/list":
            self._reply(identifier, {"tools": self._tools()})
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
    service = service or Service()
    if hasattr(service, "catalog"):  # Read the catalog while the client starts up, before it lists the tools.
        threading.Thread(target=service.catalog, daemon=True).start()
    Server(service, output).serve(sys.stdin.buffer)
    return 0
