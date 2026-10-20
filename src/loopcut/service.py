"""Loopcut's cloud tools as plain calls: sign in, quote, start, wait, download and cancel.

The CLI and the MCP server are thin layers over Service. Every call returns a JSON-ready dict
or raises LoopcutError with a message written for the person or agent reading it.

Every cloud tool goes through the same contract API (§3.4), paid from the signed-in account's
credits: a quote prices the exact request, and the job spends at most what the quote said.
Render first packs the saved .blend into a bundle; the other tools upload their file inputs,
given as local paths, by content hash. A quote lasts ten minutes; start() re-quotes an expired
one and goes ahead only if the price is unchanged. A 0-credit quote (asset search) may run
straight through. Results download, hash-checked and resumable, into loopcut_renders/<job_id>/
next to the .blend for a render and loopcut_outputs/<job_id>/ in the working folder otherwise.
"""

import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, media, motion, pack, state
from . import catalog as catalogs
from .addon import blobs, cloud_http

DEFAULT_SERVICE_URL = "https://loopcut.org"
HEADERS = {"x-loopcut-contract": "0.7", "x-loopcut-client": f"loopcut/{__version__}"}
RENDER = "render"
TERMINAL = {"succeeded", "failed", "canceled"}
POLL_SECONDS = {RENDER: 5}  # Other tools are polled every 2 s: most finish in seconds.
QUOTE_MARGIN = 30  # Seconds: a quote this close to expiry is renewed before starting.
CATALOG_MAX_AGE = 600  # Seconds before the server's catalog is read again, as the add-on does.
CATALOG_RETRY = 60  # Seconds before retrying a server whose catalog could not be read.
CATALOG_TIMEOUT = 10
DOWNLOAD_CHUNK = 1024 * 1024
LISTED = 12
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "application/json": ".json",
               "model/gltf-binary": ".glb", "model/vnd.fbx": ".fbx", "audio/wav": ".wav", "audio/x-wav": ".wav",
               "audio/mpeg": ".mp3", "video/mp4": ".mp4", "application/x-bvh": ".bvh"}
SIGN_IN_MESSAGE = "Not signed in to Loopcut, or the key was revoked. Call loopcut_login (CLI: loopcut login)."
_KEEP = {"confirm_url", "balance", "needed", "upgrade", "confirm_token", "retry_after", "field", "daily_limit",
         "spent_today", "resets_at", "plan", "data_class", "scope", "refunded", "min_client"}


class LoopcutError(RuntimeError):
    def __init__(self, message, code="error", extra=None):
        super().__init__(message)
        self.code = code
        self.extra = dict(extra or {})

    def as_dict(self):
        return {"error": {"code": self.code, "message": str(self), **self.extra}}


def _from_cloud(error, tool=None):
    """A contract error as a LoopcutError whose message says what to do next."""
    extra = {k: v for k, v in error.extra.items() if k in _KEEP}
    code, message = error.code, str(error)
    name = catalogs.agent_name(tool) if tool else "this tool"
    if code in {"sign_in_required", "invalid_api_key"}:
        message = SIGN_IN_MESSAGE
    elif code == "age_confirmation_required":
        message = ("Paid Loopcut tools need a one-time confirmation that the user is 18 or older. "
                   + (f"Ask them to confirm at {extra['confirm_url']}, then try again." if extra.get("confirm_url")
                      else "They confirm once on their Loopcut account page, then try again."))
    elif code == "insufficient_credits":
        message = (f"Not enough Loopcut credits: this needs {extra.get('needed', 'more')}, and the balance is "
                   f"{extra.get('balance', 'lower')}. The user can get a plan, or top up on Pro or Max, at "
                   f"{extra.get('upgrade') or 'their Loopcut account page'}.")
    elif code == "private_mode":
        data_class = f" (data class {extra['data_class']})" if extra.get("data_class") else ""
        message = (f"Private mode is on for this Loopcut account, and it blocks {name}: its provider may train on "
                   f"what it receives{data_class}. Nothing was charged. The user can turn Private mode off in the "
                   "Loopcut app's account settings or with `loopcut settings --private-mode off`, or do without "
                   "this tool.")
    elif code == "generation_disabled":
        message = (f"Generation is off for this Loopcut account (the no-generation setting), so generative tools "
                   f"such as {name} are refused. Nothing was charged. The user can turn generation back on in the "
                   "Loopcut app or with `loopcut settings --no-generation off`.")
    elif code == "tool_disabled":
        message = f"{name} is switched off on Loopcut right now. Nothing was charged; try again later."
    elif code == "concurrency_limit":
        message = "Too many Loopcut jobs are running at once. Wait for one to finish (job_status), then try again."
    elif code == "rate_limited" and not extra.get("confirm_token"):
        message += f" Wait {extra.get('retry_after', 60)} seconds, then try again."
    elif code == "vendor_failed":
        message += " Unspent credits were refunded; a retry is a new quote."
    elif code == "contract_unsupported":
        message = "This loopcut client is too old for the server. Update it: uvx --refresh loopcut, or pip install -U loopcut."
    elif code == "invalid_input" and extra.get("field") == "input.topology":
        # Servers before the connector counted as FBX-capable refuse quad remesh results for it.
        message = 'This Loopcut server can\'t send quad results to this client yet. Use topology: "triangles" for a GLB.'
    elif code == "invalid_input" and extra.get("field"):
        message = f"{message} (field: {extra['field']})"
    if extra.get("confirm_token"):
        message += (" If the user approves going past this limit, repeat the same call with"
                    f" confirm_token={extra['confirm_token']} (valid 10 minutes, one job).")
    return LoopcutError(message, code, extra)


def _now():
    return datetime.now(timezone.utc)


def _expired(expires_at, margin=0):
    try:
        moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return True
    return (moment - _now()).total_seconds() <= margin


def _spent_or_expired(quote):
    """Whether a finished quote task must not be handed out again: it expired, or started a job (asking
    again for the same thing is a new take, so it gets a new quote)."""
    if _expired(quote["expires_at"], QUOTE_MARGIN):
        return True
    try:
        return bool((state.load("quotes", quote["quote_id"]) or {}).get("job_id"))
    except (OSError, ValueError):
        return True


def _remaining(deadline):
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def frame_count(settings):
    start = settings.get("frame_start", 1)
    end = settings.get("frame_end", start + 119 if "turntable" in settings else start)
    return max(0, (end - start) // settings.get("frame_step", 1) + 1)


def _warnings(packed):
    lines = [f"Missing {item.get('type', 'file')}: {item.get('path') or item.get('name')}" for item in packed["missing"]]
    lines += [f"Not bundled ({item.get('reason', 'unsupported')}): {item.get('path') or item.get('name')}"
              for item in packed["unsupported"]]
    return lines


_SNIFFED = {"image/png", "image/jpeg", "image/webp", "audio/wav", "audio/x-wav", "model/gltf-binary", "model/vnd.fbx",
            "video/mp4", "video/quicktime", "video/webm"}


def _sniff(path):
    """The MIME type of a local file from its first bytes, else from its extension."""
    with open(path, "rb") as handle:
        head = handle.read(64)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"glTF"):
        return "model/gltf-binary"
    if head.startswith(b"Kaydara FBX Binary"):
        return "model/vnd.fbx"
    video = media.sniff_video(head)
    if video:
        return video
    guess = mimetypes.guess_type(Path(path).name)[0]
    if guess in _SNIFFED:
        return "application/octet-stream"  # Named like one of the types above, but its bytes are not.
    return guess or "application/octet-stream"


def _local_files(descriptor, files):
    """Check the local file inputs against the descriptor: [{field, path, size, mtime_ns, mime}]."""
    specs = {spec["field"]: spec for spec in descriptor.get("file_inputs") or []}
    found = []
    for field, value in (files or {}).items():
        if value in (None, ""):
            continue
        spec = specs.get(field)
        if spec is None or not isinstance(value, str):
            raise LoopcutError(f"{field} is not a file input of {catalogs.agent_name(descriptor['name'])}.",
                               "invalid_input", {"field": field})
        path = Path(value).expanduser().resolve()
        kinds = " or ".join(catalogs.MIME_NAMES.get(mime, mime) for mime in spec["accept"])
        try:
            stat = path.stat()
        except OSError:
            raise LoopcutError(f"No file at {path}. {field} takes a local path to a {kinds} file.", "invalid_input",
                               {"field": field}) from None
        if not path.is_file():
            raise LoopcutError(f"{path} is not a file.", "invalid_input", {"field": field})
        if stat.st_size > spec["max_mb"] * 1024 * 1024:
            raise LoopcutError(f"{path.name} is {stat.st_size / 1e6:.1f} MB; {field} takes at most {spec['max_mb']} MB.",
                               "invalid_input", {"field": field})
        mime = _sniff(path)
        if mime not in spec["accept"]:
            raise LoopcutError(f"{path.name} is not a {kinds} file, which {field} needs.", "invalid_input", {"field": field})
        found.append({"field": field, "path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "mime": mime})
    given = {item["field"] for item in found}
    for field, spec in specs.items():
        if spec.get("required") and field not in given:
            kinds = " or ".join(catalogs.MIME_NAMES.get(mime, mime) for mime in spec["accept"])
            raise LoopcutError(f"{field} is required: a local path to a {kinds} file.", "invalid_input", {"field": field})
    return found


def _apply_args(descriptor, inputs):
    """Take the descriptor's client-only apply_args (contract 0.7) out of inputs: they are kept with the quote
    and job for applying the result here, and never sent to the server."""
    schema = descriptor.get("apply_args") or {}
    properties = schema.get("properties") or {}
    found = {}
    for name, spec in properties.items():
        if name not in inputs:
            continue
        value = inputs.pop(name)
        kind = spec.get("type")
        if kind == "boolean" and type(value) is not bool or kind == "string" and not (
                isinstance(value, str) and spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", 1 << 16)):
            raise LoopcutError(f"{name} must be a {'true or false' if kind == 'boolean' else 'non-empty name'}.",
                               "invalid_input", {"field": name})
        found[name] = value
    return found


class _Task:
    """Background work (packing, uploading, downloading) that a tool call can return before, and the
    next call with the same arguments picks up where it is."""

    def __init__(self, run):
        self.stage, self.message = "starting", "Starting"
        self.result = self.error = None
        self.count = 0
        self.started = time.monotonic()
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(run,), daemon=True)
        self._thread.start()

    def say(self, message):
        self.message = message

    def _run(self, run):
        try:
            self.result = run(self)
        except Exception as ex:  # Reported to the caller on its next poll, then retried fresh.
            self.error = ex
        finally:
            self.done.set()


class Service:
    def __init__(self, base_url=None, api_key=None):
        self.base_url = (base_url or os.environ.get("LOOPCUT_SERVICE_URL") or DEFAULT_SERVICE_URL).rstrip("/")
        self._key = api_key or os.environ.get("LOOPCUT_API_KEY") or None
        self._login = None
        self._tasks = {}
        self._lock = threading.Lock()
        self._catalog = None
        self._catalog_lock = threading.Lock()
        self.on_catalog_change = None  # Called with each new Catalog (the MCP server announces tool changes).
        self.roots = []  # The client's workspace folders (MCP roots), when it shares them.

    # ------------------------------------------------------------------ HTTP

    def api_key(self):
        if self._key:
            return self._key
        stored = state.credentials(self.base_url)
        return stored["api_key"] if stored else None

    def work_dir(self):
        """The folder results go under by default: the client's first workspace root, else the working
        folder, unless that is the plugin's own install folder (Codex starts plugin servers there),
        where results would be lost on the next update: then the home folder."""
        for root in self.roots:
            if root.is_dir():
                return root
        cwd = Path.cwd().resolve()
        for name in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
            plugin = os.environ.get(name)
            if plugin:
                plugin = Path(plugin).expanduser().resolve()
                if cwd == plugin or plugin in cwd.parents:
                    return Path.home()
        return cwd

    def can_sign_in(self):
        """Whether a signed-out call may start the browser sign-in: not when LOOPCUT_API_KEY was given."""
        return not self._key

    def _require_key(self):
        """Fail before any slow work (packing, uploading) when there is no key at all."""
        if not self.api_key():
            raise LoopcutError(SIGN_IN_MESSAGE, "sign_in_required")

    def _client(self, timeout=60):
        return cloud_http.Client(base_url=self.base_url, api_key=self.api_key() or "", client_headers=HEADERS,
                                 timeout=timeout)

    def _call(self, method, path, payload=None, *, auth=True, headers=None, tool=None, timeout=60):
        try:
            return self._client(timeout).request(method, path, payload, auth=auth, headers=headers)
        except cloud_http.CloudError as ex:
            raise _from_cloud(ex, tool) from None

    def _task(self, key, run, stale=None):
        """The running or finished task for key, starting a new one if there is none or the last failed."""
        with self._lock:
            task = self._tasks.get(key)
            if task is None or task.done.is_set() and (task.error or (stale and stale(task.result))):
                task = self._tasks[key] = _Task(run)
        return task

    # ------------------------------------------------------------------ catalog

    def _manifest(self, timeout=60):
        """The manifest as this account sees it; signed out (or with a revoked key) the public one."""
        if self.api_key():
            try:
                return self._call("GET", "/v1/manifest", timeout=timeout)
            except LoopcutError as ex:
                if ex.code not in {"invalid_api_key", "sign_in_required"}:
                    raise
        return self._call("GET", "/v1/manifest", auth=False, timeout=timeout)

    def _cache_id(self):
        return hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()[:32]

    def _use_catalog(self, fresh, manifest=None):
        if manifest is not None:
            try:
                state.save("catalogs", self._cache_id(), {
                    "base_url": self.base_url, "saved_at": _now().isoformat(timespec="seconds"),
                    "manifest": {key: manifest.get(key) for key in ("tools", "capabilities", "flags")}})
            except OSError:
                pass  # The cache is only a fallback.
        self._catalog = fresh
        if self.on_catalog_change:
            self.on_catalog_change(fresh)
        return fresh

    def catalog(self, refresh=False):
        """The tool catalog: the server's (read again after 10 minutes), else the last one read from
        this server, else the snapshot shipped with the package."""
        with self._catalog_lock:
            current = self._catalog
            if current and not refresh:
                limit = CATALOG_MAX_AGE if current.source == "server" else CATALOG_RETRY
                if time.monotonic() - current.read_at < limit:
                    return current
            try:
                manifest = self._manifest(timeout=CATALOG_TIMEOUT)
                if not isinstance(manifest.get("tools"), list):
                    raise ValueError("manifest without tools")
            except (LoopcutError, OSError, ValueError):
                if current:
                    current.read_at = time.monotonic()  # Keep it; try the server again later.
                    return current
                cached = None
                try:
                    cached = state.load("catalogs", self._cache_id())
                except (OSError, ValueError):
                    pass
                if cached and cached.get("base_url") == self.base_url and isinstance(cached.get("manifest"), dict):
                    return self._use_catalog(catalogs.Catalog.from_manifest(cached["manifest"], "cache"))
                return self._use_catalog(catalogs.Catalog.from_snapshot())
            return self._use_catalog(catalogs.Catalog.from_manifest(manifest), manifest)

    def _descriptor(self, name):
        found = self.catalog().find(name)
        if found:
            return found
        known = self.catalog()
        for tool in known.tools.values():
            if name in {tool["name"], catalogs.agent_name(tool["name"])}:
                if known.settings().get("no_generation") and tool.get("kind") == "gen":
                    raise _from_cloud(cloud_http.CloudError("", "generation_disabled"), tool["name"])
                raise LoopcutError(f"{name} is not open on Loopcut yet.", "tool_disabled")
        names = ", ".join(catalogs.agent_name(tool["name"]) for tool in known.visible())
        raise LoopcutError(f"Unknown Loopcut tool {name!r}. Open tools: {names}.", "unknown_tool")

    # ------------------------------------------------------------------ account

    def login(self, wait=0, open_browser=True):
        """Sign in with the device link the add-on uses. Returns the link while it is pending."""
        if self.api_key():
            try:
                me = self._call("GET", "/v1/me")
                return {"signed_in": True, "email": me.get("email"), "plan": me.get("plan")}
            except LoopcutError as ex:
                if ex.code != "invalid_api_key" or self._key:
                    raise
                state.forget_credentials(self.base_url)
        if self._login is None or _expired(self._login["expires_at"]):
            started = self._call("POST", "/api/device/start", {"device_name": "Loopcut connector"}, auth=False)
            self._login = started
            opened = False
            if open_browser:
                try:
                    opened = webbrowser.open(started["verification_url"])
                except webbrowser.Error:
                    pass  # The link is in the result for the user to open by hand.
            if not opened:
                wait = 0  # Nobody sees a link until this call returns it.
        pending = self._login
        deadline = time.monotonic() + max(0, wait)
        while True:
            polled = self._call("POST", "/api/device/poll", {"device_code": pending["device_code"]}, auth=False)
            status = polled.get("status")
            if status == "approved" and isinstance(polled.get("api_key"), str):
                state.save_credentials(self.base_url, polled["api_key"], polled.get("email"))
                self._login = None
                self.catalog(refresh=True)  # The account's own view: settings and availability.
                return {"signed_in": True, "email": polled.get("email")}
            if status in {"denied", "expired"}:
                self._login = None
                raise LoopcutError(f"Sign-in was {status}. Call loopcut_login again for a new link.", f"login_{status}")
            if time.monotonic() >= deadline:
                return {"signed_in": False, "verification_url": pending["verification_url"],
                        "user_code": pending["user_code"], "expires_at": pending["expires_at"],
                        "next": ("Ask the user to open verification_url, sign in to Loopcut and approve code "
                                 f"{pending['user_code']}; then call loopcut_login again to finish.")}
            time.sleep(max(0.5, min(int(pending.get("interval") or 5), deadline - time.monotonic())))

    def logout(self):
        state.forget_credentials(self.base_url)
        return {"signed_in": False}

    def account(self):
        self._require_key()
        me = self._call("GET", "/v1/me")
        manifest = self._manifest()
        known = self._use_catalog(catalogs.Catalog.from_manifest(manifest), manifest)
        account = manifest.get("account") or {}
        credits, settings = account.get("credits") or {}, account.get("settings") or {}
        tools = [{"tool": catalogs.agent_name(tool["name"]), "available": tool.get("available"),
                  "unavailable_reason": tool.get("unavailable_reason")}
                 for tool in sorted(known.tools.values(), key=lambda tool: tool["name"])
                 if tool.get("status") in catalogs.OPEN]
        notes = []
        blocked = [row["tool"] for row in tools if row["unavailable_reason"] == "private_mode"]
        if settings.get("private_mode"):
            notes.append("Private mode is on: tools whose provider may train on what it receives are refused"
                         + (f" ({', '.join(blocked)})" if blocked else "")
                         + ". Turn it off in the Loopcut app or with `loopcut settings --private-mode off`.")
        if settings.get("no_generation"):
            notes.append("Generation is off: generative tools are hidden and refused. Turn it back on in the Loopcut "
                         "app or with `loopcut settings --no-generation off`.")
        return {"email": me.get("email"), "plan": me.get("plan") or account.get("plan"),
                "credits": credits.get("balance"), "credits_monthly": credits.get("monthly"),
                "credits_reset_at": credits.get("resets_at"),
                "settings": {"private_mode": settings.get("private_mode"), "no_generation": settings.get("no_generation")},
                "tools": tools, "notes": notes, "price": "1 credit = $0.01. Every paid tool quotes its price first."}

    def settings(self, *, private_mode=None, no_generation=None):
        """Change the account's Private mode or no-generation setting (the user's call, from the CLI)."""
        patch = {key: value for key, value in (("private_mode", private_mode), ("no_generation", no_generation))
                 if value is not None}
        self._require_key()
        if not patch:
            return self.account()["settings"]
        result = self._call("PATCH", "/v1/settings", patch)
        self.catalog(refresh=True)
        return result

    # ------------------------------------------------------------------ quotes

    def quote(self, blend, settings, *, blender=None, wait=None, confirm_token=None):
        """Pack, upload and price a render. wait=None blocks; otherwise returns progress after
        `wait` seconds and the same call later picks the work up where it is."""
        settings = {key: value for key, value in (settings or {}).items() if value is not None}
        self._require_key()
        path = Path(blend).expanduser().resolve()
        try:
            stat = path.stat()
        except OSError:
            raise LoopcutError(f"No .blend file at {path}. Save the scene first; the cloud renders the file on disk.",
                               "invalid_input", {"field": "blend"}) from None
        key = json.dumps(["render", str(path), stat.st_size, stat.st_mtime_ns, blender or "", settings], sort_keys=True)
        task = self._task(key, lambda job: self._prepare_render(job, path, settings, blender, confirm_token),
                          stale=_spent_or_expired)
        if not task.done.wait(wait):
            return {"status": "preparing", "stage": task.stage, "message": task.message,
                    "elapsed_s": round(time.monotonic() - task.started),
                    "next": "Packing and uploading is still running; call render_quote again with the same arguments."}
        if task.error:
            raise task.error
        return task.result

    def _prepare_render(self, task, path, settings, blender, confirm_token):
        task.stage = "checking"
        descriptor = self._descriptor(RENDER)
        multipart = blobs.multipart_capability(self.catalog().capabilities.get("blob_multipart"))
        task.stage = "packing"
        try:
            packed = pack.pack_and_upload(path, client=self._client(timeout=120), blender=blender, multipart=multipart,
                                          progress=task.say)
        except cloud_http.CloudError as ex:
            raise _from_cloud(ex, RENDER) from None
        except (pack.PackError, ValueError) as ex:
            raise LoopcutError(str(ex), "pack_failed") from None
        task.stage = "quoting"
        project = {"id": "blend-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24], "no_generation": False}
        record = {"body": {"tool": RENDER, "input": settings, "bundle": packed["bundle"]}, "tool_version": descriptor["version"],
                  "blend": str(path), "packed": packed, "project": project}
        return self._new_quote(record, confirm_token)

    def quote_tool(self, name, inputs=None, files=None, *, confirm_token=None, wait=None, out_dir=None):
        """Upload a tool's file inputs and price the request. A 0-credit quote runs straight through:
        the result is then the finished job with its outputs, if it ends within `wait` seconds."""
        deadline = None if wait is None else time.monotonic() + max(0, wait)
        descriptor = self._descriptor(name)
        if descriptor["name"] == RENDER:
            raise LoopcutError("Render needs a .blend: use render_quote (CLI: loopcut render).", "invalid_input")
        inputs = {key: value for key, value in (inputs or {}).items() if value is not None}
        apply_args = _apply_args(descriptor, inputs)
        local = _local_files(descriptor, files)
        if descriptor["name"] == motion.VIDEO_TOOL and local:
            try:
                inputs = motion.capture_inputs(inputs, local[0]["path"], local[0]["mime"])
            except (motion.MotionError, media.MediaError) as ex:
                raise LoopcutError(str(ex), "invalid_input", {"field": "video_seconds"}) from None
        self._require_key()
        key = json.dumps(["tool", descriptor["name"], inputs, local, apply_args], sort_keys=True)
        task = self._task(key, lambda job: self._prepare_tool(job, descriptor, inputs, local, confirm_token, apply_args),
                          stale=_spent_or_expired)
        if not task.done.wait(_remaining(deadline)):
            return {"status": "preparing", "stage": task.stage, "message": task.message,
                    "elapsed_s": round(time.monotonic() - task.started),
                    "next": f"Uploading is still running; call {catalogs.agent_name(descriptor['name'])} again with the same arguments."}
        if task.error:
            raise task.error
        quote = task.result
        if quote["credits"] != 0:
            return quote
        return self.start(quote["quote_id"], confirm_token=confirm_token, wait=_remaining(deadline), out_dir=out_dir)

    def _prepare_tool(self, task, descriptor, inputs, local, confirm_token, apply_args=None):
        files = {}
        if local:
            task.stage = "uploading"
            multipart = blobs.multipart_capability(self.catalog().capabilities.get("blob_multipart"))
            items = []
            for item in local:
                task.say(f"Hashing {Path(item['path']).name}")
                digest = blobs.sha256(item["path"], max_bytes=blobs.MAX_BLOB_SIZE)
                files[item["field"]] = digest
                items.append({"path": item["path"], "sha256": digest, "size": item["size"], "mime": item["mime"]})
            try:
                blobs.upload_missing(items, client=self._client(timeout=120), multipart=multipart, progress=task.say)
            except cloud_http.CloudError as ex:
                raise _from_cloud(ex, descriptor["name"]) from None
            except ValueError as ex:
                raise LoopcutError(str(ex), "invalid_input") from None
        task.stage = "quoting"
        body = {"tool": descriptor["name"], "input": inputs, **({"files": files} if files else {})}
        project = {"id": "dir-" + hashlib.sha256(str(self.work_dir()).encode("utf-8")).hexdigest()[:24], "no_generation": False}
        record = {"body": body, "tool_version": descriptor["version"], "project": project,
                  "local_files": {item["field"]: item["path"] for item in local}}
        if apply_args:
            record["apply_args"] = apply_args  # Client-only (contract 0.7): kept for applying, never sent.
        return self._new_quote(record, confirm_token)

    def _new_quote(self, record, confirm_token=None):
        headers = {"x-loopcut-confirm": confirm_token} if confirm_token else None
        quote = self._call("POST", "/v1/quotes", record["body"], headers=headers, tool=record["body"]["tool"])
        record = {**record, "quote_id": quote["quote_id"], "credits": quote["credits"], "expires_at": quote["expires_at"],
                  "idempotency_key": uuid.uuid4().hex, "created_at": _now().isoformat(timespec="seconds")}
        if confirm_token:
            record["confirm_token"] = confirm_token  # The user's approval covers this request's job too.
        state.save("quotes", record["quote_id"], record)
        return self._quote_result(record)

    @staticmethod
    def _quote_result(record):
        body = record["body"]
        result = {"status": "quoted", "tool": catalogs.agent_name(body["tool"]), "quote_id": record["quote_id"],
                  "credits": record["credits"], "usd": round(record["credits"] / 100, 2), "expires_at": record["expires_at"]}
        if body["tool"] == RENDER:
            packed, settings = record["packed"], body["input"]
            result.update({"frames": frame_count(settings), "settings": settings, "scene": packed["scene"],
                           "bundle": {"files": packed["files"], "mb": round(packed["bytes"] / 1e6, 1),
                                      "packed_with_blender": packed["blender"]},
                           "warnings": _warnings(packed)})
            result["next"] = ("Tell the user the price; once they agree, call job_start with this quote_id. The quote "
                              "lasts 10 minutes and is the most the job can cost: it charges actual GPU time.")
        else:
            result["input"] = body["input"]
            if record.get("apply_args"):
                result["apply_args"] = record["apply_args"]
            result["next"] = ("Tell the user the price; once they agree (or it is within a budget they gave), call "
                              "job_start with this quote_id. The quote lasts 10 minutes and is the most the job can cost.")
        return result

    # ------------------------------------------------------------------ jobs

    def _quote_record(self, quote_id):
        record = state.load("quotes", quote_id)
        for _ in range(5):  # An expired quote renewed at the same price points at its renewal.
            if record is None or not record.get("renewed_as") or record.get("job_id"):
                break
            record = state.load("quotes", record["renewed_as"]) or record
        if record is None:
            raise LoopcutError(f"Unknown quote {quote_id}. Get a quote first (render_quote or the tool's own call).",
                               "not_found")
        return record

    def start(self, quote_id, *, confirm_token=None, wait=0, out_dir=None, download=True):
        """Start a quoted job, which spends credits. Repeating it returns the same job. With `wait`
        (None: until it ends), it also waits and, if download, fetches the outputs."""
        deadline = None if wait is None else time.monotonic() + max(0, wait)
        self._require_key()
        record = self._quote_record(quote_id)
        if record.get("job_id"):
            job = self._call("GET", f"/v1/jobs/{_job_id(record['job_id'])}")
            return self._follow(job, deadline, download=download, out_dir=out_dir)
        original = record
        token = confirm_token or record.get("confirm_token")
        headers = {"x-loopcut-confirm": token} if token else None
        tool = record["body"]["tool"]
        for attempt in (1, 2):  # The second only after the server called the quote expired.
            if attempt == 2 or _expired(record["expires_at"], QUOTE_MARGIN):
                renewed = self._new_quote({key: record[key] for key in record if key in {
                    "body", "tool_version", "blend", "packed", "project", "local_files", "apply_args"}}, token)
                if renewed["credits"] != record["credits"]:
                    return {**renewed, "status": "requoted",
                            "next": "The quote expired and the price changed. Confirm the new price, then call job_start with the new quote_id."}
                state.save("quotes", record["quote_id"], {**record, "renewed_as": renewed["quote_id"]})
                record = state.load("quotes", renewed["quote_id"])
            body = {"tool": tool, "tool_version": record.get("tool_version", 1), "input": record["body"]["input"],
                    "files": record["body"].get("files") or {}, "quote_id": record["quote_id"],
                    "project": record.get("project") or {"id": "dir-default", "no_generation": False},
                    "idempotency_key": record["idempotency_key"]}
            if record["body"].get("bundle"):
                body["bundle"] = record["body"]["bundle"]
            try:
                job = self._call("POST", "/v1/jobs", body, headers=headers, tool=tool)
                break
            except LoopcutError as ex:
                if ex.code != "quote_expired" or attempt == 2:
                    raise
        if out_dir:
            target = Path(out_dir).expanduser().resolve()
        elif tool == RENDER and record.get("blend"):
            target = Path(record["blend"]).parent / "loopcut_renders" / job["id"]
        else:
            target = self.work_dir() / ("loopcut_renders" if tool == RENDER else "loopcut_outputs") / job["id"]
        state.save("jobs", job["id"], {"job_id": job["id"], "quote_id": record["quote_id"], "tool": tool,
                                       "blend": record.get("blend"), "out_dir": str(target),
                                       "credits_quoted": record["credits"], "created_at": _now().isoformat(timespec="seconds"),
                                       **({"apply_args": record["apply_args"]} if record.get("apply_args") else {}),
                                       **({"title": _title(record)} if _title(record) else {})})
        for done in dict.fromkeys((original["quote_id"], record["quote_id"])):
            state.save("quotes", done, {**(state.load("quotes", done) or record), "job_id": job["id"]})
        return self._follow(job, deadline, download=download, out_dir=None)

    def status(self, job_id, wait=0, *, download=False, out_dir=None):
        """A job's status, waiting up to `wait` seconds for it to end. With download, a finished
        job's outputs are fetched in the same call (and the next one, if time runs out)."""
        deadline = None if wait is None else time.monotonic() + max(0, wait)
        job = self._call("GET", f"/v1/jobs/{_job_id(job_id)}")
        return self._follow(job, deadline, download=download, out_dir=out_dir)

    def _follow(self, job, deadline, *, download, out_dir):
        poll = POLL_SECONDS.get(job.get("tool"), 2)
        while job.get("status") not in TERMINAL:
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                break
            time.sleep(poll if remaining is None else min(poll, remaining))
            job = self._call("GET", f"/v1/jobs/{_job_id(job.get('id'))}")
        result = self._job_result(job)
        if download and job.get("status") == "succeeded":
            result.update(self.fetch(job["id"], out_dir, wait=_remaining(deadline), job=job))
        return result

    def cancel(self, job_id):
        return self._job_result(self._call("POST", f"/v1/jobs/{_job_id(job_id)}/cancel"))

    @staticmethod
    def _job_result(job):
        status, tool = job.get("status"), job.get("tool")
        result = {"job_id": job.get("id"), "tool": catalogs.agent_name(tool) if tool else None, "status": status,
                  "progress": job.get("progress"), "credits": job.get("credits")}
        if job.get("error"):
            result["error"] = {"code": job["error"].get("code"), "message": job["error"].get("message")}
        partial = job.get("partial") if status == "succeeded" else None
        if isinstance(partial, dict):
            result["partial"] = partial
        result["next"] = {
            "succeeded": (f"Only part finished: {partial.get('message')} For the rest, quote and start a new job with the same input "
                          "plus partial.resume_input. " if isinstance(partial, dict) else "") + "Call job_status to download the results.",
            "failed": "Unspent credits were refunded. Fix the cause, then quote again: a retry is a new quote and job.",
            "canceled": "Unspent credits were refunded.",
        }.get(status, "Still running; call job_status with wait_seconds to wait for it."
              + (" A first render can spend several minutes starting a GPU worker." if tool == RENDER else ""))
        return result

    def fetch(self, job_id, out_dir=None, wait=None, *, job=None):
        """Download a finished job's outputs. Files already present with the right hash are kept, so
        a call that runs out of time carries on in the background and the next call picks it up."""
        if job is None or job.get("status") != "succeeded":
            job = self._call("GET", f"/v1/jobs/{_job_id(job_id)}")
        if job.get("status") != "succeeded":
            return {**self._job_result(job), "downloaded": False}
        record = state.load("jobs", job["id"]) or {}
        if out_dir:
            out = Path(out_dir).expanduser().resolve()
        elif record.get("out_dir"):
            out = Path(record["out_dir"])
        else:
            out = self.work_dir() / ("loopcut_renders" if job.get("tool") == RENDER else "loopcut_outputs") / job["id"]
        key = json.dumps(["fetch", job["id"], str(out)])
        task = self._task(key, lambda work: self._download_all(work, job, out, record))
        if not task.done.wait(wait):
            return {"job_id": job["id"], "downloading": True, "out_dir": str(out), "files_saved": task.count,
                    "files_total": len(job.get("outputs") or []), "message": task.message,
                    "next": "Still downloading; call job_status again (finished files are kept)."}
        with self._lock:
            if self._tasks.get(key) is task:
                del self._tasks[key]  # The next fetch checks the files on disk again.
        if task.error:
            raise task.error
        return task.result

    # ------------------------------------------------------------------ motion

    def motion_apply(self, blend, bvh, *, armature=None, in_place=None, frame_start=None, out=None, blender=None,
                     wait=None):
        """Put a motion result on the rig in a saved .blend with a local headless Blender, and save it. Free: no
        account needed. armature and in_place default to what the motion tool was called with. Returns while
        Blender is still working after `wait` seconds; the next call with the same arguments picks it up."""
        try:
            bvh = motion.find_bvh(bvh)
            path = Path(blend).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() != ".blend":
                raise motion.MotionError(f"Not a saved .blend file: {path}. Save the scene first.")
            pack.find_blender(blender)
        except (motion.MotionError, pack.PackError) as ex:
            raise LoopcutError(str(ex), "invalid_input") from None
        record = _motion_job(bvh)
        kept = record.get("apply_args") or {}
        armature = armature if armature else kept.get("target_armature")
        in_place = in_place if in_place is not None else kept.get("in_place") is True
        out = str(Path(out).expanduser().resolve()) if out else None
        blender = str(blender) if blender else None
        if frame_start is not None and type(frame_start) is not int:
            raise LoopcutError("frame_start must be a whole frame number.", "invalid_input", {"field": "frame_start"})

        def run(work):
            try:
                return motion.apply_blend(path, bvh, armature=armature, in_place=in_place, frame_start=frame_start,
                                          title=record.get("title"), out=out, blender=blender, progress=work.say)
            except motion.MotionError as ex:
                raise LoopcutError(str(ex), "apply_failed") from None

        key = json.dumps(["motion_apply", str(path), str(bvh), armature, in_place, frame_start, out, blender])
        task = self._task(key, run)
        if not task.done.wait(wait):
            return {"status": "applying", "message": task.message, "elapsed_s": round(time.monotonic() - task.started),
                    "next": "Blender is still applying the motion; call motion_apply again with the same arguments."}
        with self._lock:
            if self._tasks.get(key) is task:
                del self._tasks[key]  # A later call with the same arguments applies again.
        if task.error:
            raise task.error
        return task.result

    def motion_snippet(self, bvh, *, armature=None, in_place=None):
        """Python that applies a motion result inside a Blender that is already open (motion-apply --bpy)."""
        try:
            bvh = motion.find_bvh(bvh)
        except motion.MotionError as ex:
            raise LoopcutError(str(ex), "invalid_input", {"field": "bvh"}) from None
        record = _motion_job(bvh)
        kept = record.get("apply_args") or {}
        return motion.bpy_snippet(bvh, armature=armature or kept.get("target_armature"),
                                  in_place=in_place if in_place is not None else kept.get("in_place") is True,
                                  title=record.get("title"))

    def _download_all(self, task, job, out, record=None):
        out.mkdir(parents=True, exist_ok=True)
        client = self._client(timeout=120)
        outputs = job.get("outputs") or []
        saved = []
        for index, output in enumerate(outputs):
            target = out / _output_name(output, index)
            if not _matches(target, output):
                task.say(f"Downloading {target.name} ({index + 1} of {len(outputs)})")
                try:
                    _download(client, output, target)
                except cloud_http.CloudError as ex:
                    raise _from_cloud(ex, job.get("tool")) from None
            saved.append((output, target))
            task.count = len(saved)
        return _outputs_result(job, out, saved, record)


def _motion_job(bvh):
    """The connector's record of the job that saved this motion.bvh (in loopcut_outputs/<job_id>/), if any."""
    name = Path(bvh).parent.name
    if not re.fullmatch(r"job_[A-Za-z0-9_-]{1,120}", name):
        return {}
    try:
        return state.load("jobs", name) or {}
    except (OSError, ValueError):
        return {}


def _title(record):
    """A motion's name in Blender, as the add-on names it: the prompt, else the video's file name."""
    prompt = (record.get("body") or {}).get("input", {}).get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    video = (record.get("local_files") or {}).get("video")
    return Path(video).stem if video else None


def _job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"job_[A-Za-z0-9_-]{1,120}", value):
        raise LoopcutError(f"Invalid job id: {value!r}", "invalid_input", {"field": "job_id"})
    return value


def _output_name(output, index):
    """Output names are server data: keep a plain file name, adding the MIME type's extension."""
    name, extension = output.get("name"), _EXTENSIONS.get(output.get("mime"), ".bin")
    if not (isinstance(name, str) and _SAFE_NAME.fullmatch(name) and ".." not in name):
        name = f"output_{index:04d}"
    return name if Path(name).suffix else name + extension


def _matches(target, output):
    try:
        return target.stat().st_size == output.get("size") and blobs.sha256(target) == output.get("sha256")
    except OSError:
        return False


def _download(client, output, target):
    """Stream a signed output URL to a temporary file, verify size and hash, then move it in place."""
    temporary = target.with_name(f".{target.name}.part")
    digest, size = hashlib.sha256(), 0
    try:
        with client.download(output["url"]) as response, temporary.open("wb") as handle:
            for chunk in iter(lambda: response.read(DOWNLOAD_CHUNK), b""):
                size += len(chunk)
                if size > output["size"]:
                    break
                digest.update(chunk)
                handle.write(chunk)
        if size != output["size"] or digest.hexdigest() != output["sha256"]:
            raise LoopcutError(f"Downloaded {target.name} does not match its hash; call job_status again.",
                               "download_mismatch")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _outputs_result(job, out, saved, record=None):
    tool = job.get("tool")
    result = {"job_id": job["id"], "out_dir": str(out), "files_saved": len(saved)}
    report = next((target for output, target in saved if output.get("type") == "report"), None)
    if tool == RENDER:
        frames = [str(target) for output, target in saved if output.get("type") == "render_frames"]
        result["frames"] = frames[:LISTED]
        if len(frames) > LISTED:
            result["frames_total"] = len(frames)
        if report:
            result["qa"] = _qa_summary(report)
        result["next"] = "Done. Look at the frames (and the QA report's flagged frames) before telling the user it is finished."
        return result
    if tool == "asset_search" and report:
        result.update(_asset_summary(report))
        return result
    result["files"] = [{"path": str(target), "type": output.get("type"), "mime": output.get("mime"),
                        **({"meta": output["meta"]} if output.get("meta") else {})} for output, target in saved][:LISTED]
    if tool in motion.TOOLS or any(output.get("type") == "motion" for output, _ in saved):
        record = record or {}
        result.update(motion.result_for([target for _, target in saved], record.get("apply_args"), record.get("title")))
        return result
    result["next"] = _import_hint(output.get("mime") for output, _ in saved)
    return result


def _import_hint(mimes):
    hints = {
        "model/gltf-binary": "import the GLB with bpy.ops.import_scene.gltf(filepath=path)",
        "model/vnd.fbx": "import the FBX with bpy.ops.import_scene.fbx(filepath=path)",
        "audio/wav": "add the WAV to the sequencer as a sound strip (scene.sequence_editor_create(), then strips.new_sound)",
        "audio/mpeg": "add the MP3 to the sequencer as a sound strip",
        "image/png": "load the PNG with bpy.data.images.load(path) as a texture, reference or backplate",
        "application/json": "read the JSON report",
    }
    found = [hints[mime] for mime in dict.fromkeys(mimes) if mime in hints]
    return ("Done. To use it in Blender, " + "; ".join(found) + ".") if found else "Done."


def _qa_summary(path):
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"summary": "The QA report could not be read."}
    flagged = report.get("flagged") if isinstance(report.get("flagged"), list) else []
    flags = {row.get("frame"): row.get("flags") for row in report.get("frames") or []
             if isinstance(row, dict) and row.get("flags")}
    return {"summary": report.get("summary"), "flagged_frames": flagged[:LISTED],
            "flags": {str(frame): flags.get(frame) for frame in flagged[:LISTED]}, "report": str(path)}


def _asset_summary(path):
    """The asset search report, trimmed to what an agent needs to pick and fetch an asset."""
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"next": "The search report could not be read."}
    results = []
    for row in report.get("results") or []:
        if not isinstance(row, dict):
            continue
        item = {key: row.get(key) for key in ("id", "title", "kind") if row.get(key) is not None}
        item["license"] = (row.get("license") or {}).get("id")
        if (row.get("attribution") or {}).get("required"):
            item["attribution"] = row["attribution"].get("text")
        for key in ("polycount", "dimensions_m", "max_resolution", "page_url", "preview_url"):
            if row.get(key) is not None:
                item[key] = row[key]
        if row.get("downloads"):
            item["downloads"] = [{key: entry.get(key) for key in ("label", "format", "size", "url")}
                                 for entry in row["downloads"][:4] if isinstance(entry, dict)]
        if row.get("source") == "polyhaven" and row.get("source_id"):
            item["files_api"] = f"https://api.polyhaven.com/files/{row['source_id']}"
        library = row.get("remote_library")
        if isinstance(library, dict) and library.get("entries"):
            item["blender_library"] = {"url": library.get("url"), "entries": [
                {"name": entry.get("name"), "id_type": entry.get("id_type"), "file": entry.get("file"),
                 "urls": [f.get("url") for f in (entry.get("files") or [])[:3] if isinstance(f, dict)]}
                for entry in library["entries"][:3] if isinstance(entry, dict)]}
        results.append(item)
    credits = [source.get("credit") for source in report.get("sources") or []
               if isinstance(source, dict) and source.get("credit_required") and source.get("credit")]
    summary = {"query": report.get("query"), "mode": report.get("mode"), "count": len(results), "results": results,
               "report": str(path)}
    if credits:
        summary["show_credit"] = credits
    if report.get("notices"):
        summary["notices"] = report["notices"]
    summary["next"] = ("Show the user the matches (and show_credit next to them). To use one, download a file from "
                       "downloads, the Poly Haven files_api or blender_library, then import it with bpy: "
                       "bpy.ops.import_scene.gltf for glTF/GLB, bpy.data.libraries.load for .blend, "
                       "bpy.data.images.load for textures and HDRIs.") if results else (
        "No matches. Try other words or kinds, or generate the asset instead.")
    return summary
