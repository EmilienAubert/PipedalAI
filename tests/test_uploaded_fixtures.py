from __future__ import annotations

import json
import os
import unittest
import zipfile
from pathlib import Path


class UploadedPresetCompatibilityTests(unittest.TestCase):
    def test_reference_preset_shapes(self):
        default = Path(__file__).parent / "fixtures" / "reference-presets"
        root = Path(os.environ.get("PIPEDAL_AI_REFERENCE_PRESETS", str(default)))
        fixtures = list(root.glob("*.piPreset")) if root.is_dir() else []
        if not fixtures:
            self.skipTest("Set PIPEDAL_AI_REFERENCE_PRESETS to test uploaded PiPedal fixtures")
        for path in fixtures:
            with self.subTest(path=path.name), zipfile.ZipFile(path) as archive:
                self.assertIn("bankFile.json", archive.namelist())
                self.assertIn("pluginsUsed.json", archive.namelist())
                bank = json.loads(archive.read("bankFile.json"))
                preset = bank["presets"][0]["preset"]
                self.assertIn("items", preset)
                for item in preset["items"]:
                    for key in ("instanceId", "uri", "isEnabled", "controlValues", "lv2State", "pathProperties"):
                        self.assertIn(key, item)


if __name__ == "__main__": unittest.main()
