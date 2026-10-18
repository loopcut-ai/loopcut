"""Refresh src/loopcut/catalog.json, the tool catalog the connector falls back to when it can't
read the server's at startup. Run it before a release:

    python3 scripts/snapshot_catalog.py [https://loopcut.org]

It reads /v1/manifest signed out, as this client sees it, and keeps every descriptor without
the per-account fields (available, unavailable_reason).
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "loopcut" / "catalog.json"
ACCOUNT_FIELDS = {"available", "unavailable_reason"}


def main(base_url="https://loopcut.org"):
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "loopcut" / "__init__.py").read_text()).group(1)
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/manifest", headers={
        "x-loopcut-contract": "0.6", "x-loopcut-client": f"loopcut/{version}", "accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        manifest = json.load(response)
    tools = sorted(({k: v for k, v in tool.items() if k not in ACCOUNT_FIELDS} for tool in manifest["tools"]),
                   key=lambda tool: tool["name"])
    snapshot = {"source": base_url.rstrip("/") + "/v1/manifest",
                "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "capabilities": manifest.get("capabilities") or {}, "tools": tools}
    TARGET.write_text(json.dumps(snapshot, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(tools)} tools ({', '.join(t['name'] + ':' + t['status'] for t in tools)}) -> {TARGET}")


if __name__ == "__main__":
    main(*sys.argv[1:])
