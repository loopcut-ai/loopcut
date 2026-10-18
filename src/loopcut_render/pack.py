"""Pack a saved .blend into a render bundle with any stock Blender, then upload it.

Collection is the add-on's own: a factory-startup, background Blender child with script
autoexecution off runs addon/bundles.py as a script, which copies every texture, library and
cache the scene reads into a staging folder with relative paths and saves scene.blend there.
The child gets a private config folder and temp folder and none of the parent's environment
beyond PATH, HOME and locale, so no Loopcut key reaches it.

The manifest's source is {"kind": "job"} (contract §3.3): this bundle is not an add-on
checkpoint. Everything after collection uses only the standard library.
"""

import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .addon import blobs, bundles

PACK_TIMEOUT = 900  # Seconds for one Blender collection run, as long as a heavy scene's save.
KEEP_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
LOG_TAIL = 4000


class PackError(RuntimeError):
    pass


def _candidates():
    yield os.environ.get("LOOPCUT_BLENDER")
    yield os.environ.get("BLENDER")
    yield shutil.which("blender")
    if sys.platform == "darwin":
        for root in ("/Applications", os.path.expanduser("~/Applications")):
            yield f"{root}/Blender.app/Contents/MacOS/Blender"
            yield from sorted(glob.glob(f"{root}/Blender*.app/Contents/MacOS/Blender"), reverse=True)
        yield "/Applications/Loopcut.app/Contents/MacOS/Loopcut"
    elif sys.platform == "win32":
        for root in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"))):
            yield from sorted(glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe")), reverse=True)
    else:
        yield "/snap/bin/blender"


def find_blender(explicit=None):
    """The Blender binary to pack with: the argument, LOOPCUT_BLENDER, BLENDER, PATH, then the
    usual install folders."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir() and path.suffix == ".app":
            path = next(iter(sorted((path / "Contents" / "MacOS").glob("*"))), path)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise PackError(f"Blender not found at {explicit}")
        return str(path)
    for candidate in _candidates():
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise PackError("Blender not found. Install Blender, or set LOOPCUT_BLENDER to its executable.")


def _tail(log):
    with log.open("rb") as handle:
        handle.seek(max(0, handle.seek(0, os.SEEK_END) - LOG_TAIL))
        return handle.read(LOG_TAIL).decode("utf-8", errors="replace")


def collect(blend, staging, blender):
    """Run the add-on's collector on a saved .blend into an empty staging folder.

    Returns {blender, scene, created_at, missing, unsupported, system} (bundles._collect_opened).
    """
    blend = Path(blend).expanduser().resolve()
    if not blend.is_file() or blend.suffix.lower() != ".blend":
        raise PackError(f"Not a saved .blend file: {blend}")
    with tempfile.TemporaryDirectory(prefix="loopcut-render-pack-") as temporary:
        control = Path(temporary)
        for name in ("tmp", "config", "scripts"):
            (control / name).mkdir()
        env = {name: os.environ[name] for name in KEEP_ENV if os.environ.get(name)}
        env.update(TMPDIR=str(control / "tmp"), TEMP=str(control / "tmp"), TMP=str(control / "tmp"),
                   BLENDER_USER_CONFIG=str(control / "config"), BLENDER_USER_SCRIPTS=str(control / "scripts"))
        request, reply, log = control / "request.json", control / "reply.json", control / "blender.log"
        request.write_text(json.dumps({"blend": str(blend), "staging": str(staging), "original": None}),
                           encoding="utf-8")
        command = [blender, "-b", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
                   "--python", str(Path(bundles.__file__).resolve()), "--", str(request), str(reply)]
        try:
            with log.open("wb") as output:
                completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                           env=env, timeout=PACK_TIMEOUT)
        except subprocess.TimeoutExpired as ex:
            raise PackError(f"Blender took longer than {PACK_TIMEOUT} s to collect the scene") from ex
        except OSError as ex:
            raise PackError(f"Could not run Blender at {blender}: {ex.strerror or ex}") from ex
        if completed.returncode or not reply.is_file():
            raise PackError("Blender could not collect the scene:\n" + _tail(log))
        result = json.loads(reply.read_text(encoding="utf-8"))
    if not isinstance(result, dict) or not (Path(staging) / bundles.ENTRY).is_file():
        raise PackError("Blender returned an invalid bundle result")
    return result


def manifest(staging, info):
    """The contract bundle manifest for a collected staging folder (SPEC §3.3)."""
    root = bundles._root(staging)
    files = [{"path": name, "sha256": blobs.sha256(path, max_bytes=blobs.MAX_BLOB_SIZE), "size": path.stat().st_size}
             for name, path in bundles._staged_files(root)]
    return {"schema": bundles.SCHEMA, "blender": info["blender"], "entry": bundles.ENTRY,
            "files": sorted(files, key=lambda item: item["path"]),
            "scene": {key: info["scene"][key] for key in ("name", "camera", "frame_start", "frame_end", "fps")},
            "source": {"kind": "job"},
            "created_at": info["created_at"]}


def canonical_json(value):
    """The bytes the server hashes: sorted keys, no spaces, whole floats written as integers."""
    return json.dumps(bundles._js_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def upload(staging, value, *, client, multipart=None, progress=None):
    """Upload the members the server lacks, then the manifest; return the manifest's SHA-256."""
    root = bundles._root(staging)
    files = [{**item, "path": root / item["path"]} for item in value["files"]]
    blobs.upload_missing(files, client=client, progress=progress, multipart=multipart)
    body = canonical_json(value)
    digest = hashlib.sha256(body).hexdigest()
    with tempfile.TemporaryDirectory(prefix="loopcut-render-manifest-") as temporary:
        path = Path(temporary) / "manifest.json"
        path.write_bytes(body)
        blobs.upload_missing([{"path": path, "sha256": digest, "size": len(body), "mime": "application/json"}],
                             client=client, progress=progress)
    return digest


def pack_and_upload(blend, *, client, blender=None, multipart=None, progress=None):
    """Collect, hash and upload a saved .blend. Returns the bundle hash and what was collected."""
    binary = find_blender(blender)
    say = progress or (lambda message: None)
    with tempfile.TemporaryDirectory(prefix="loopcut-render-bundle-") as staging:
        say("Collecting the scene and its files with Blender…")
        info = collect(blend, staging, binary)
        value = manifest(staging, info)
        total = sum(item["size"] for item in value["files"])
        say(f"Uploading {len(value['files'])} files ({total / 1e6:.1f} MB) that the cloud does not have yet…")
        digest = upload(staging, value, client=client, multipart=multipart, progress=say)
    return {"bundle": digest, "blender": info["blender"], "blender_path": binary, "scene": value["scene"],
            "files": len(value["files"]), "bytes": total,
            "missing": info.get("missing") or [], "unsupported": info.get("unsupported") or []}
