"""Every place that names the release version agrees (the publish workflow checks the tag the same way)."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class VersionsTest(unittest.TestCase):
    def test_versions_match(self):
        project = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M).group(1)
        server = json.loads(_read("server.json"))
        found = {
            "__init__.py": re.search(r'__version__ = "([^"]+)"', _read("src/loopcut_render/__init__.py")).group(1),
            "plugin.json": json.loads(_read(".claude-plugin/plugin.json"))["version"],
            "marketplace.json": json.loads(_read(".claude-plugin/marketplace.json"))["plugins"][0]["version"],
            "server.json": server["version"],
            "server.json package": server["packages"][0]["version"],
        }
        self.assertEqual({name: project for name in found}, found)

    def test_readme_names_the_registry_entry(self):
        name = json.loads(_read("server.json"))["name"]
        self.assertIn(f"mcp-name: {name}", _read("README.md"))


if __name__ == "__main__":
    unittest.main()
