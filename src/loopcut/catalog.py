"""The Loopcut tool catalog: which cloud tools are open, their input schemas and file inputs.

The server's GET /v1/manifest is the source (it answers signed out too), so a tool that opens
on the server shows up here without a client release. When it can't be read, the last catalog
read from the same server (kept in the state folder) stands in, then the snapshot shipped with
the package (catalog.json, refreshed by scripts/snapshot_catalog.py before each release).
"""

import json
import re
import time
from pathlib import Path

OPEN = {"live", "beta"}
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HERE = Path(__file__).parent
AGENT_TOOLS = json.loads((_HERE / "agent_tools.json").read_text(encoding="utf-8"))["tools"]
# Availability reasons that mean the account can't use the tool at all; the tool is hidden.
_HIDDEN_REASONS = {"disabled", "region"}
MIME_NAMES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP", "model/gltf-binary": "GLB",
              "model/vnd.fbx": "FBX", "audio/wav": "WAV", "audio/mpeg": "MP3", "video/mp4": "MP4"}


def snapshot():
    """The catalog shipped with this release: {source, taken_at, capabilities, tools}."""
    return json.loads((_HERE / "catalog.json").read_text(encoding="utf-8"))


def agent_name(tool_name):
    entry = AGENT_TOOLS.get(tool_name)
    return entry["name"] if entry else tool_name


def _order(descriptor):
    names = list(AGENT_TOOLS)
    name = descriptor["name"]
    return (names.index(name) if name in names else len(names), name)


def usable(descriptor, account=None):
    """Whether the agent sees this tool: open on the server, buildable by this client, and allowed
    by the account (no-generation hides generative tools, as the contract asks of every client)."""
    if not isinstance(descriptor.get("input_schema"), dict) or descriptor.get("status") not in OPEN:
        return False
    if not TOOL_NAME.fullmatch(agent_name(descriptor["name"])):
        return False  # MCP clients and directories take names of up to 64 letters, digits, _ and -.
    if descriptor.get("available") is False and descriptor.get("unavailable_reason") in _HIDDEN_REASONS:
        return False
    bundle = descriptor.get("bundle_input")
    if bundle and bundle.get("required") and descriptor["name"] != "render":
        return False  # Only render's scene bundle can be built here.
    settings = (account or {}).get("settings") or {}
    return not (settings.get("no_generation") and descriptor.get("kind") == "gen")


class Catalog:
    """One reading of the catalog. source is "server", "cache" or "snapshot"."""

    def __init__(self, tools, source, *, account=None, capabilities=None, flags=None):
        self.tools = {tool["name"]: tool for tool in tools or []
                      if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
        self.source = source
        self.account = account if isinstance(account, dict) else None
        self.capabilities = capabilities if isinstance(capabilities, dict) else {}
        self.flags = flags if isinstance(flags, dict) else {}
        self.read_at = time.monotonic()

    @classmethod
    def from_manifest(cls, manifest, source="server"):
        return cls(manifest.get("tools"), source, account=manifest.get("account"),
                   capabilities=manifest.get("capabilities"), flags=manifest.get("flags"))

    @classmethod
    def from_snapshot(cls):
        value = snapshot()
        return cls(value["tools"], "snapshot", capabilities=value.get("capabilities"))

    def visible(self):
        """Descriptors of the tools the agent sees, render first."""
        return sorted((tool for tool in self.tools.values() if usable(tool, self.account)), key=_order)

    def find(self, name):
        """The visible descriptor for an agent-facing or catalog tool name, or None."""
        for tool in self.visible():
            if name in {tool["name"], agent_name(tool["name"])}:
                return tool
        return None

    def settings(self):
        return dict((self.account or {}).get("settings") or {})
