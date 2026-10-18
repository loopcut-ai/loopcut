"""Small JSON records on disk: the signed-in key, quotes awaiting a start, started jobs and the
last tool catalog read from the server.

Everything lives under LOOPCUT_CONNECTOR_HOME, else %APPDATA%/loopcut-connector on Windows and
$XDG_CONFIG_HOME/loopcut-connector (default ~/.config/loopcut-connector) elsewhere. Not
~/.config/loopcut: other Loopcut tools already keep files there, and the Blender add-on keeps its
own sign-in in Blender's config folder, so this folder belongs to the connector alone. Files are
written whole, owner-only, and replaced atomically, so the CLI and an MCP server can share them.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
FOLDER = "loopcut-connector"


def home():
    explicit = os.environ.get("LOOPCUT_CONNECTOR_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / FOLDER
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / FOLDER


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, ensure_ascii=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return value if isinstance(value, dict) else None


def _record(kind, identifier):
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise ValueError(f"Invalid {kind[:-1]} id: {identifier!r}")
    return home() / kind / f"{identifier}.json"


def credentials(base_url):
    """{api_key, email} stored for this service, or None."""
    entry = (_read(home() / "credentials.json") or {}).get(base_url)
    return entry if isinstance(entry, dict) and isinstance(entry.get("api_key"), str) else None


def save_credentials(base_url, api_key, email):
    stored = _read(home() / "credentials.json") or {}
    stored[base_url] = {"api_key": api_key, "email": email}
    _write(home() / "credentials.json", stored)


def forget_credentials(base_url):
    stored = _read(home() / "credentials.json") or {}
    if stored.pop(base_url, None) is not None:
        _write(home() / "credentials.json", stored)


def load(kind, identifier):
    return _read(_record(kind, identifier))


def save(kind, identifier, value):
    _write(_record(kind, identifier), value)
