from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pipedal_ai.catalog import CatalogService
from pipedal_ai.compiler import PresetCompiler
from pipedal_ai.db import Database
from pipedal_ai.degraded import DegradedProposer, _choose_asset, _find_plugin, _prompt_tags


def descriptor(uri: str, name: str, controls: list[dict]) -> str:
    ports = [
        {"index": 0, "symbol": "in", "name": "In", "direction": "input", "kind": "audio", "datatype": "number", "minimum": None, "maximum": None, "default": None, "properties": [], "scale_points": [], "supported_events": []},
        {"index": 1, "symbol": "out", "name": "Out", "direction": "output", "kind": "audio", "datatype": "number", "minimum": None, "maximum": None, "default": None, "properties": [], "scale_points": [], "supported_events": []},
    ] + [{"index": i + 2, "name": p["symbol"], "direction": "input", "kind": "control", "properties": [], "scale_points": [], "supported_events": [], **p} for i, p in enumerate(controls)]
    return json.dumps({"uri": uri, "name": name, "author": "test", "class": "Plugin", "has_latency": False, "ports": ports})


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "catalog.db")
        self.db.initialize()
        content = b"NAM fixture\n"
        asset = self.root / "uploads/NeuralAmpModels/model.nam"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        plugins = [
            ("plg_111111111111111111111111", "http://two-play.com/plugins/toob-input_stage", "TooB Input Stage", [{"symbol": "trim", "datatype": "number", "minimum": -60, "maximum": 30, "default": 0}]),
            ("plg_222222222222222222222222", "http://two-play.com/plugins/toob-nam", "TooB Neural Amp Modeler", [{"symbol": "inputGain", "datatype": "number", "minimum": -40, "maximum": 40, "default": 0}]),
            ("plg_333333333333333333333333", "http://two-play.com/plugins/toob-volume", "TooB Volume", [{"symbol": "vol", "datatype": "number", "minimum": -60, "maximum": 30, "default": 0}]),
        ]
        with self.db.transaction() as c:
            c.execute("INSERT INTO catalog_revisions VALUES(1,?,?,?,?,?,?,?,?,?,?)", ("a"*64,"pipedal-ai.catalog-source/1.0.0","pipedal-ai.inventory/1.0.0","b"*64,"test","2026-01-01T00:00:00Z","2026-01-01T00:00:00Z",3,1,0))
            for pid, uri, name, controls in plugins:
                value = descriptor(uri, name, controls)
                c.execute("INSERT INTO catalog_plugins VALUES(1,?,?,?,?,?,?,?,?)", (pid,uri,name,"Plugin","test",0,"c"*64,value))
            c.execute("INSERT INTO catalog_assets VALUES(1,?,?,?,?,?,?)", ("ast_111111111111111111111111","nam","NeuralAmpModels/model.nam",".nam",len(content),digest))
            c.execute("INSERT INTO catalog_state VALUES(1,1)")
        self.catalog = CatalogService(self.db)

    def tearDown(self): self.temp.cleanup()

    def test_capabilities_hide_local_paths(self):
        encoded = json.dumps(self.catalog.capabilities())
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("relative_path", encoded)

    def test_degraded_three_variants_validate(self):
        value = DegradedProposer().propose("req", "warm blues crunch", self.catalog.capabilities())
        self.assertEqual([p.variant for p in value.proposals], ["conservative", "balanced", "bold"])
        self.catalog.validate_proposal_set(value)

    def test_french_prompt_is_normalized(self):
        tags = _prompt_tags("Son clair, chaud et légèrement saturé, avec une pièce légère")
        self.assertTrue({"clean", "warm", "crunch", "space"}.issubset(tags))
        self.assertNotIn("high_gain", tags)

    def test_plugin_priority_does_not_depend_on_catalog_order(self):
        capabilities = {
            "plugins": [
                {"plugin_id": "club", "name": "GxClubDrive", "uri": "clubdrive"},
                {"plugin_id": "ts", "name": "GxTubeScreamer", "uri": "gxts9"},
            ]
        }
        selected = _find_plugin(capabilities, "gxtubescreamer", "clubdrive")
        self.assertEqual(selected["plugin_id"], "ts")

    def test_french_tone_selects_matching_nam(self):
        capabilities = {
            "assets": [
                {"asset_id": "clean", "kind": "nam", "display_name": "Orange AD30 Cleanest"},
                {"asset_id": "crunch", "kind": "nam", "display_name": "Fender Tweed Deluxe Crunch Warm"},
                {"asset_id": "metal", "kind": "nam", "display_name": "EVH 5150 Metal Lead"},
            ]
        }
        selected = _choose_asset(capabilities, "nam", "blues chaud avec un crunch léger")
        self.assertEqual(selected["asset_id"], "crunch")

    def test_compiler_embeds_verified_media_and_pipedal_shape(self):
        spec = DegradedProposer().propose("req", "clean", self.catalog.capabilities()).proposals[1]
        compiler = PresetCompiler(self.catalog, self.root / "uploads", 1_000_000)
        result = compiler.compile(spec, self.root / "artifacts")
        second = compiler.compile(spec, self.root / "artifacts-2")
        self.assertEqual(result["sha256"], second["sha256"])
        self.assertRegex(result["sha256"], r"^[a-f0-9]{64}$")
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual(set(archive.namelist()), {"bankFile.json", "pluginsUsed.json", "media/NeuralAmpModels/model.nam"})
            bank = json.loads(archive.read("bankFile.json"))
            item = next(x for x in bank["presets"][0]["preset"]["items"] if x["uri"].endswith("toob-nam"))
            self.assertTrue(item["lv2State"][0])
            state_entry = item["lv2State"][1]["http://two-play.com/plugins/toob-nam#modelFile"]
            self.assertEqual(list(state_entry), ["flags", "atomType", "value"])
            self.assertEqual(state_entry["value"], "NeuralAmpModels/model.nam")

    def test_catalog_mismatch_is_rejected(self):
        value = DegradedProposer().propose("req", "clean", self.catalog.capabilities())
        value.proposals[0].catalog.sha256 = "d" * 64
        with self.assertRaises(Exception): self.catalog.validate_proposal_set(value)


if __name__ == "__main__": unittest.main()
