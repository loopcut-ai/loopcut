"""Small JSON records on disk: the signed-in key, quotes awaiting a start, and started jobs.

Everything lives under LOOPCUT_RENDER_HOME, else %APPDATA%/loopcut-render on Windows and
$XDG_CONFIG_HOME/loopcut-render (default ~/.config/loopcut-render) elsewhere. Files are
written whole, owner-only, and replaced atomically, so the CLI and an MCP server can share them.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def home():
    explicit = os.environ.get("LOOPCUT_RENDER_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "loopcut-render"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "loopcut-render"


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
