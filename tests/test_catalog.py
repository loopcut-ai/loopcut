"""The catalog snapshot shipped as the fallback, and how catalog tools are named for the agent."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loopcut import catalog

FIXTURES = Path(__file__).resolve().parents[3] / "contract" / "fixtures" / "tool-descriptor"


class Snapshot(unittest.TestCase):
    def test_snapshot_is_a_full_catalog(self):
        value = catalog.snapshot()
        names = [tool["name"] for tool in value["tools"]]
        self.assertEqual(names, sorted(names))
        for tool in value["tools"]:
            with self.subTest(tool["name"]):
                self.assertIn(tool["status"], {"live", "beta", "coming_soon"})
                self.assertEqual(tool["input_schema"]["type"], "object")
                self.assertNotIn("available", tool, "per-account fields are left out")
        self.assertIn("render", {tool["name"] for tool in catalog.Catalog.from_snapshot().visible()})

    @unittest.skipUnless(FIXTURES.is_dir(), "contract fixtures not available outside loopcut-web")
    def test_snapshot_schemas_match_the_contract(self):
        for tool in catalog.snapshot()["tools"]:
            fixture = json.loads((FIXTURES / f"{tool['name']}.json").read_text(encoding="utf-8"))
            for key in ("version", "kind", "input_schema", "file_inputs", "bundle_input", "output"):
                with self.subTest(tool=tool["name"], key=key):
                    self.assertEqual(tool.get(key), fixture.get(key))

    def test_every_open_tool_has_an_agent_name_and_description(self):
        for tool in catalog.snapshot()["tools"]:
            if tool["status"] in catalog.OPEN or tool["name"] in {"image_generate", "image_edit"}:
                with self.subTest(tool["name"]):
                    entry = catalog.AGENT_TOOLS[tool["name"]]
                    self.assertRegex(entry["name"], r"^[a-z][a-z0-9_]{2,40}$")
                    self.assertLess(len(entry["description"]), 400)
        self.assertEqual(catalog.agent_name("retopology"), "remesh")
        self.assertEqual(catalog.agent_name("sfx"), "sound_effect")
        self.assertEqual(catalog.agent_name("something_new"), "something_new")

    def test_find_by_either_name(self):
        known = catalog.Catalog.from_snapshot()
        self.assertEqual(known.find("remesh")["name"], "retopology")
        self.assertEqual(known.find("retopology")["name"], "retopology")
        self.assertIsNone(known.find("image_generate"), "coming_soon tools are not offered")


class WorkDir(unittest.TestCase):
    def test_results_avoid_the_plugin_install_folder(self):
        from loopcut import service
        with tempfile.TemporaryDirectory() as plugin:
            previous = os.getcwd()
            os.chdir(plugin)
            try:
                with mock.patch.dict(os.environ, {"PLUGIN_ROOT": plugin}):
                    self.assertEqual(service.Service(base_url="http://127.0.0.1:9").work_dir(), Path.home())
                with mock.patch.dict(os.environ, {"PLUGIN_ROOT": "", "CLAUDE_PLUGIN_ROOT": ""}):
                    self.assertEqual(service.Service(base_url="http://127.0.0.1:9").work_dir(), Path(plugin).resolve())
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
