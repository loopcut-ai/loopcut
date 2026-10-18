"""Loopcut cloud render as plain calls: sign in, quote, start, wait, fetch and cancel.

The CLI and the MCP server are thin layers over Service. Every call returns a JSON-ready dict
or raises RenderError with a message written for the person or agent reading it.

Credits come from the signed-in Loopcut account (contract §3.4): a quote prices the exact
request, and the job spends only what that quote said. A quote lasts ten minutes; start()
re-quotes an expired one and goes ahead only if the price is unchanged.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, pack, state
from .addon import blobs, cloud_http

DEFAULT_SERVICE_URL = "https://loopcut.org"
HEADERS = {"x-loopcut-contract": "0.6", "x-loopcut-client": f"loopcut-render/{__version__}"}
TOOL, TOOL_VERSION = "render", 1
TERMINAL = {"succeeded", "failed", "canceled"}
POLL_SECONDS = 5
QUOTE_MARGIN = 30  # Seconds: a quote this close to expiry is renewed before starting.
DOWNLOAD_CHUNK = 1024 * 1024
LISTED_FILES = 12
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EXTENSIONS = {"image/png": ".png", "application/json": ".json"}


class RenderError(RuntimeError):
    def __init__(self, message, code="error", extra=None):
        super().__init__(message)
        self.code = code
        self.extra = dict(extra or {})

    def as_dict(self):
        return {"error": {"code": self.code, "message": str(self), **self.extra}}


def _from_cloud(error):
    """A contract error as a RenderError whose message says what to do next."""
    extra = {k: v for k, v in error.extra.items()
             if k in {"confirm_url", "balance", "needed", "upgrade", "confirm_token", "retry_after", "field",
                      "daily_limit", "spent_today", "resets_at", "plan", "data_class"}}
    message = str(error)
    if error.code == "sign_in_required" or error.code == "invalid_api_key":
        message = "Not signed in to Loopcut, or the key was revoked. Call loopcut_login (CLI: loopcut-render login)."
    elif error.code == "age_confirmation_required" and extra.get("confirm_url"):
        message += f" Ask the user to confirm once at {extra['confirm_url']}, then try again."
    elif error.code == "insufficient_credits":
        message += " The user can add credits or upgrade on their Loopcut account page."
    elif extra.get("confirm_token"):
        message += (" If the user approves going past this limit, repeat the same call with"
                    f" confirm_token={extra['confirm_token']} (valid 10 minutes, one job).")
    return RenderError(message, error.code, extra)


def _now():
    return datetime.now(timezone.utc)


def _expired(expires_at, margin=0):
    try:
        moment = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return True
    return (moment - _now()).total_seconds() <= margin


def frame_count(settings):
    start = settings.get("frame_start", 1)
    end = settings.get("frame_end", start + 119 if "turntable" in settings else start)
    return max(0, (end - start) // settings.get("frame_step", 1) + 1)


def _warnings(packed):
    lines = [f"Missing {item.get('type', 'file')}: {item.get('path') or item.get('name')}" for item in packed["missing"]]
    lines += [f"Not bundled ({item.get('reason', 'unsupported')}): {item.get('path') or item.get('name')}"
              for item in packed["unsupported"]]
    return lines


class _Prepare:
    """One background pack, upload and quote, so a tool call can return before it finishes."""

    def __init__(self, run):
        self.stage, self.message = "starting", "Starting"
        self.result = self.error = None
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
        self._prepares = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ HTTP

    def api_key(self):
        if self._key:
            return self._key
        stored = state.credentials(self.base_url)
        return stored["api_key"] if stored else None

    def _client(self, timeout=60):
        return cloud_http.Client(base_url=self.base_url, api_key=self.api_key() or "", client_headers=HEADERS,
                                 timeout=timeout)

    def _call(self, method, path, payload=None, *, auth=True, headers=None):
        try:
            return self._client().request(method, path, payload, auth=auth, headers=headers)
        except cloud_http.CloudError as ex:
            raise _from_cloud(ex) from None

    # ------------------------------------------------------------------ account

    def login(self, wait=0, open_browser=True):
        """Sign in with the device link the add-on uses. Returns the link while it is pending."""
        if self.api_key():
            try:
                me = self._call("GET", "/v1/me")
                return {"signed_in": True, "email": me.get("email"), "plan": me.get("plan")}
            except RenderError as ex:
                if ex.code != "invalid_api_key" or self._key:
                    raise
                state.forget_credentials(self.base_url)
        if self._login is None or _expired(self._login["expires_at"]):
            started = self._call("POST", "/api/device/start", {"device_name": "Loopcut Render"}, auth=False)
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
                return {"signed_in": True, "email": polled.get("email")}
            if status in {"denied", "expired"}:
                self._login = None
                raise RenderError(f"Sign-in was {status}. Call loopcut_login again for a new link.", f"login_{status}")
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
        me = self._call("GET", "/v1/me")
        manifest = self._call("GET", "/v1/manifest")
        render = next((tool for tool in manifest.get("tools") or [] if tool.get("name") == TOOL), {})
        credits = (manifest.get("account") or {}).get("credits") or {}
        return {"email": me.get("email"), "plan": me.get("plan"), "credits": credits.get("balance"),
                "credits_monthly": credits.get("monthly"), "credits_reset_at": credits.get("resets_at"),
                "render": {"status": render.get("status"), "available": render.get("available"),
                           "unavailable_reason": render.get("unavailable_reason")},
                "price": "1 credit = $0.01; render costs 5 credits per GPU-minute, one-minute minimum"}

    # ------------------------------------------------------------------ quote

    def quote(self, blend, settings, *, blender=None, wait=None, confirm_token=None):
        """Pack, upload and price a render. wait=None blocks; otherwise returns progress after
        `wait` seconds and the same call later picks the work up where it is."""
        settings = {key: value for key, value in (settings or {}).items() if value is not None}
        path = Path(blend).expanduser().resolve()
        try:
            stat = path.stat()
        except OSError:
            raise RenderError(f"No .blend file at {path}. Save the scene first; the cloud renders the file on disk.",
                              "invalid_input", {"field": "blend"}) from None
        key = json.dumps([str(path), stat.st_size, stat.st_mtime_ns, blender or "", settings], sort_keys=True)
        with self._lock:
            task = self._prepares.get(key)
            stale = task and task.done.is_set() and (task.error or _expired(task.result["expires_at"], QUOTE_MARGIN))
            if task is None or stale:
                task = self._prepares[key] = _Prepare(
                    lambda job: self._prepare(job, path, settings, blender, confirm_token))
        if not task.done.wait(wait):
            return {"status": "preparing", "stage": task.stage, "message": task.message,
                    "elapsed_s": round(time.monotonic() - task.started),
                    "next": "Packing and uploading is still running; call render_quote again with the same arguments."}
        if task.error:
            raise task.error
        return task.result

    def _prepare(self, task, path, settings, blender, confirm_token):
        task.stage = "checking"
        manifest = self._call("GET", "/v1/manifest")
        render = next((tool for tool in manifest.get("tools") or [] if tool.get("name") == TOOL), None)
        if not (manifest.get("flags") or {}).get("services", True) or not render or render.get("status") == "coming_soon":
            raise RenderError("Cloud render is not open on this Loopcut server right now.", "tool_disabled")
        multipart = blobs.multipart_capability((manifest.get("capabilities") or {}).get("blob_multipart"))
        task.stage = "packing"
        try:
            packed = pack.pack_and_upload(path, client=self._client(timeout=120), blender=blender, multipart=multipart,
                                          progress=task.say)
        except cloud_http.CloudError as ex:
            raise _from_cloud(ex) from None
        except (pack.PackError, ValueError) as ex:
            raise RenderError(str(ex), "pack_failed") from None
        task.stage = "quoting"
        body = {"tool": TOOL, "input": settings, "bundle": packed["bundle"]}
        record = {"body": body, "blend": str(path), "packed": packed}
        return self._new_quote(record, confirm_token)

    def _new_quote(self, record, confirm_token=None):
        headers = {"x-loopcut-confirm": confirm_token} if confirm_token else None
        quote = self._call("POST", "/v1/quotes", record["body"], headers=headers)
        record = {**record, "quote_id": quote["quote_id"], "credits": quote["credits"], "expires_at": quote["expires_at"],
                  "idempotency_key": uuid.uuid4().hex, "created_at": _now().isoformat(timespec="seconds")}
        state.save("quotes", record["quote_id"], record)
        return self._quote_result(record)

    @staticmethod
    def _quote_result(record):
        packed, settings = record["packed"], record["body"]["input"]
        return {"status": "quoted", "quote_id": record["quote_id"], "credits": record["credits"],
                "usd": round(record["credits"] / 100, 2), "expires_at": record["expires_at"],
                "frames": frame_count(settings), "settings": settings, "scene": packed["scene"],
                "bundle": {"files": packed["files"], "mb": round(packed["bytes"] / 1e6, 1),
                           "packed_with_blender": packed["blender"]},
                "warnings": _warnings(packed),
                "next": ("Tell the user the price; once they agree, call render_start with this quote_id. "
                         "The quote lasts 10 minutes and is an estimate: the job charges actual GPU time.")}

    # ------------------------------------------------------------------ jobs

    def start(self, quote_id, *, confirm_token=None):
        record = state.load("quotes", quote_id)
        if record is None:
            raise RenderError(f"Unknown quote {quote_id}. Call render_quote first.", "not_found")
        if record.get("job_id"):
            return self.status(record["job_id"])
        headers = {"x-loopcut-confirm": confirm_token} if confirm_token else None
        for attempt in (1, 2):  # The second only after the server called the quote expired.
            if attempt == 2 or _expired(record["expires_at"], QUOTE_MARGIN):
                renewed = self._renew(record, confirm_token)
                if renewed["credits"] != record["credits"]:
                    return {**renewed, "status": "requoted",
                            "next": "The quote expired and the price changed. Confirm the new price, then call render_start with the new quote_id."}
                record = state.load("quotes", renewed["quote_id"])
            body = {"tool": TOOL, "tool_version": TOOL_VERSION, "input": record["body"]["input"], "files": {},
                    "bundle": record["body"]["bundle"], "quote_id": record["quote_id"],
                    "project": {"id": "blend-" + hashlib.sha256(record["blend"].encode("utf-8")).hexdigest()[:24],
                                "no_generation": False},
                    "idempotency_key": record["idempotency_key"]}
            try:
                job = self._call("POST", "/v1/jobs", body, headers=headers)
                break
            except RenderError as ex:
                if ex.code != "quote_expired" or attempt == 2:
                    raise
        state.save("jobs", job["id"], {"job_id": job["id"], "quote_id": record["quote_id"], "blend": record["blend"],
                                       "credits_quoted": record["credits"], "created_at": _now().isoformat(timespec="seconds")})
        state.save("quotes", record["quote_id"], {**record, "job_id": job["id"]})
        return self._job_result(job)

    def _renew(self, record, confirm_token):
        keep = {key: record[key] for key in ("body", "blend", "packed")}
        return self._new_quote(keep, confirm_token)

    def status(self, job_id, wait=0):
        deadline = time.monotonic() + max(0, wait)
        while True:
            job = self._call("GET", f"/v1/jobs/{_job_id(job_id)}")
            remaining = deadline - time.monotonic()
            if job.get("status") in TERMINAL or remaining <= 0:
                return self._job_result(job)
            time.sleep(min(POLL_SECONDS, remaining))

    def cancel(self, job_id):
        return self._job_result(self._call("POST", f"/v1/jobs/{_job_id(job_id)}/cancel"))

    @staticmethod
    def _job_result(job):
        status = job.get("status")
        frames = [o for o in job.get("outputs") or [] if o.get("type") == "render_frames"]
        result = {"job_id": job.get("id"), "status": status, "progress": job.get("progress"),
                  "credits": job.get("credits"), "frames_ready": len(frames)}
        if job.get("error"):
            result["error"] = {"code": job["error"].get("code"), "message": job["error"].get("message")}
        result["next"] = {
            "succeeded": "Call render_fetch to download the frames and the QA report.",
            "failed": "Unspent credits were refunded. Fix the cause, then quote and start a new render.",
            "canceled": "Unspent credits were refunded.",
        }.get(status, "Still rendering; call render_status with wait_seconds to wait for it.")
        return result

    def fetch(self, job_id, out_dir=None, wait=None):
        """Download a finished job's frames and QA report. Files already present with the right
        hash are skipped, so a call that runs out of time continues where it stopped."""
        job = self._call("GET", f"/v1/jobs/{_job_id(job_id)}")
        if job.get("status") != "succeeded":
            return {**self._job_result(job), "downloaded": False}
        record = state.load("jobs", job["id"]) or {}
        if out_dir:
            out = Path(out_dir).expanduser()
        elif record.get("blend"):
            out = Path(record["blend"]).parent / "loopcut_renders" / job["id"]
        else:
            out = Path.cwd() / "loopcut_renders" / job["id"]
        out.mkdir(parents=True, exist_ok=True)
        deadline = None if wait is None else time.monotonic() + wait
        client = self._client()
        saved, pending, report = [], 0, None
        for index, output in enumerate(job.get("outputs") or []):
            target = out / _output_name(output, index)
            if not _matches(target, output):
                if deadline is not None and time.monotonic() >= deadline:
                    pending += 1
                    continue
                try:
                    _download(client, output, target)
                except cloud_http.CloudError as ex:
                    raise _from_cloud(ex) from None
            saved.append(str(target))
            if output.get("type") == "report":
                report = target
        result = {"job_id": job["id"], "out_dir": str(out), "files_saved": len(saved), "files_pending": pending,
                  "frames": [path for path in saved if path.endswith(".png")][:LISTED_FILES]}
        if report:
            result["qa"] = _qa_summary(report)
        result["next"] = ("Some files are still to download; call render_fetch again." if pending else
                          "Done. Look at the frames (and the QA report's flagged frames) before telling the user it is finished.")
        return result


def _job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"job_[A-Za-z0-9_-]{1,120}", value):
        raise RenderError(f"Invalid job id: {value!r}", "invalid_input", {"field": "job_id"})
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
            raise RenderError(f"Downloaded {target.name} does not match its hash; call render_fetch again.",
                              "download_mismatch")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _qa_summary(path):
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"summary": "The QA report could not be read."}
    flagged = report.get("flagged") if isinstance(report.get("flagged"), list) else []
    flags = {row.get("frame"): row.get("flags") for row in report.get("frames") or []
             if isinstance(row, dict) and row.get("flags")}
    return {"summary": report.get("summary"), "flagged_frames": flagged[:LISTED_FILES],
            "flags": {str(frame): flags.get(frame) for frame in flagged[:LISTED_FILES]}, "report": str(path)}
