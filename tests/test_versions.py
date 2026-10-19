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
            "__init__.py": re.search(r'__version__ = "([^"]+)"', _read("src/loopcut/__init__.py")).group(1),
            "plugin.json": json.loads(_read(".claude-plugin/plugin.json"))["version"],
            "marketplace.json": json.loads(_read(".claude-plugin/marketplace.json"))["plugins"][0]["version"],
            "server.json": server["version"],
            "server.json package": server["packages"][0]["version"],
            "plugin.json (OpenAI)": json.loads(_read("plugin.json"))["version"],
            "uv.lock": re.search(r'name = "loopcut"\nversion = "([^"]+)"', _read("uv.lock")).group(1),
        }
        self.assertEqual({name: project for name in found}, found)

    def test_both_plugin_formats_start_the_same_server(self):
        claude = json.loads(_read(".mcp.json"))["mcpServers"]["loopcut"]
        openai = json.loads(_read("mcp.json"))["mcpServers"]["loopcut"]
        self.assertEqual(openai["type"], "stdio")
        self.assertEqual([claude["command"], *claude["args"]],
                         [openai["command"], *(a.replace("${PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}") for a in openai["args"])])
        # The plugin directory's lint wants a pinned registry launcher (uvx <package>==<version>) for
        # the Verified badge; the pin must be this release, so a version bump moves it too.
        project = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M).group(1)
        self.assertEqual([claude["command"], *claude["args"]], ["uvx", f"loopcut=={project}", "mcp"])
        marketplace = json.loads(_read(".agents/plugins/marketplace.json"))
        self.assertEqual((marketplace["name"], marketplace["plugins"][0]["name"]), ("loopcut", "loopcut"))
        interface = json.loads(_read("plugin.json"))["extensions"]["com.openai"]["interface"]
        for key in ("logo", "composerIcon"):
            self.assertTrue((ROOT / interface[key]).is_file(), interface[key])

    def test_readme_has_a_privacy_policy_section(self):
        readme = _read("README.md")
        self.assertIn("\n## Privacy Policy\n", readme)
        self.assertIn("https://loopcut.org/privacy", readme)

    def test_readme_names_the_registry_entry(self):
        name = json.loads(_read("server.json"))["name"]
        self.assertIn(f"mcp-name: {name}", _read("README.md"))


if __name__ == "__main__":
    unittest.main()
