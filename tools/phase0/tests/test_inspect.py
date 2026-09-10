import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "pipedal_ai_inspect.py"
SPEC = importlib.util.spec_from_file_location("pipedal_ai_inspect", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
inspect = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inspect)


LV2INFO_SAMPLE = """http://example.test/plugins/gain

\tName:              Test Gain
\tClass:             Amplifier Plugin
\tAuthor:            PiPedal AI tests
\tHas latency:       no
\tBundle:            file:///tmp/TestGain.lv2/
\tRequired Features: http://lv2plug.in/ns/ext/urid#map
\tOptional Features: http://lv2plug.in/ns/lv2core#hardRTCapable

\tPort 0:
\t\tType:        http://lv2plug.in/ns/lv2core#InputPort
\t\t             http://lv2plug.in/ns/lv2core#AudioPort

\t\tSymbol:      in
\t\tName:        Input

\tPort 1:
\t\tType:        http://lv2plug.in/ns/lv2core#InputPort
\t\t             http://lv2plug.in/ns/lv2core#ControlPort

\t\tScale Points:
\t\t\t0 = "Off"
\t\t\t1 = "On"

\t\tSymbol:      enabled
\t\tName:        Enabled
\t\tMinimum:     0.000000
\t\tMaximum:     1.000000
\t\tDefault:     1.000000
\t\tProperties:  http://lv2plug.in/ns/lv2core#integer
\t\t             http://lv2plug.in/ns/lv2core#enumeration

\tPort 2:
\t\tType:        http://lv2plug.in/ns/lv2core#OutputPort
\t\t             http://lv2plug.in/ns/lv2core#AudioPort

\t\tSymbol:      out
\t\tName:        Output
"""


LV2INFO_WITH_UI_AND_PRESETS = """http://example.test/plugins/cab

\tName:              Test Cab
\tClass:             Simulator Plugin
\tAuthor:            PiPedal AI tests
\tHas latency:       no
\tUIs:
\t\thttp://example.test/plugins/cab-ui
\t\t\tClass:  http://lv2plug.in/ns/extensions/ui#X11UI
\tRequired Features: http://lv2plug.in/ns/ext/urid#map
\tOptional Features: http://lv2plug.in/ns/lv2core#hardRTCapable
\tPresets:
\t         Clean
\t         Crunch

\tPort 0:
\t\tType:        http://lv2plug.in/ns/lv2core#InputPort
\t\t             http://lv2plug.in/ns/lv2core#AudioPort
\t\tSymbol:      in
\t\tName:        Input
"""


class Lv2InfoParserTests(unittest.TestCase):
    def test_parses_plugin_and_control_port(self):
        plugin = inspect.parse_lv2info(LV2INFO_SAMPLE, "http://example.test/plugins/gain")
        self.assertEqual(plugin["reported_uri"], "http://example.test/plugins/gain")
        self.assertEqual(plugin["name"], "Test Gain")
        self.assertEqual(plugin["class"], "Amplifier Plugin")
        self.assertFalse(plugin["has_latency"])
        self.assertEqual(len(plugin["ports"]), 3)

        control = plugin["ports"][1]
        self.assertEqual(control["symbol"], "enabled")
        self.assertEqual(control["direction"], "input")
        self.assertEqual(control["kind"], "control")
        self.assertEqual(control["datatype"], "enumeration")
        self.assertEqual(control["minimum"], 0)
        self.assertEqual(control["maximum"], 1)
        self.assertEqual(control["default"], 1)
        self.assertEqual(control["scale_points"], [{"value": 0, "label": "Off"}, {"value": 1, "label": "On"}])

    def test_does_not_mix_ui_or_presets_into_plugin_metadata(self):
        plugin = inspect.parse_lv2info(
            LV2INFO_WITH_UI_AND_PRESETS, "http://example.test/plugins/cab"
        )
        self.assertEqual(plugin["class"], "Simulator Plugin")
        self.assertEqual(
            plugin["required_features"], ["http://lv2plug.in/ns/ext/urid#map"]
        )
        self.assertEqual(
            plugin["optional_features"],
            ["http://lv2plug.in/ns/lv2core#hardRTCapable"],
        )

    def test_warns_when_lv2_default_is_out_of_range(self):
        plugin = inspect.parse_lv2info(LV2INFO_SAMPLE, "http://example.test/plugins/gain")
        plugin["ports"][1]["minimum"] = 1
        plugin["ports"][1]["maximum"] = 2
        plugin["ports"][1]["default"] = 0
        warnings = []
        inspect.warn_about_plugin_metadata(plugin, warnings)
        self.assertEqual([item["code"] for item in warnings], ["LV2_DEFAULT_OUT_OF_RANGE"])


class AssetScannerTests(unittest.TestCase):
    def test_hashes_files_and_safe_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary)
            nam_root = data_root / "audio_uploads" / "NeuralAmpModels"
            cab_root = data_root / "audio_uploads" / "CabIR"
            nam_root.mkdir(parents=True)
            cab_root.mkdir(parents=True)

            model = nam_root / "Amp Model.nam"
            model.write_bytes(b"nam-test-data")
            (nam_root / "notes.txt").write_text("ignore", encoding="utf-8")
            (cab_root / "Cab.wav").write_bytes(b"wave-test-data")

            symlink_supported = True
            try:
                os.symlink(model, nam_root / "Alias.nam")
            except (OSError, NotImplementedError):
                symlink_supported = False

            warnings = []
            result = inspect.scan_assets(data_root, warnings, throttle_mib_per_sec=0)
            expected_count = 3 if symlink_supported else 2
            self.assertEqual(result["asset_count"], expected_count)
            paths = [item["relative_path"] for item in result["items"]]
            expected_paths = ["CabIR/Cab.wav", "NeuralAmpModels/Amp Model.nam"]
            if symlink_supported:
                expected_paths.insert(1, "NeuralAmpModels/Alias.nam")
            self.assertEqual(paths, expected_paths)

            nam = next(item for item in result["items"] if item["kind"] == "nam")
            self.assertEqual(nam["sha256"], hashlib.sha256(b"nam-test-data").hexdigest())
            self.assertEqual(warnings, [])

    def test_accepts_only_explicitly_trusted_external_symlink_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            data_root = base / "data"
            cab_root = data_root / "audio_uploads" / "CabIR"
            cab_root.mkdir(parents=True)
            trusted_root = base / "factory" / "CabIR"
            trusted_root.mkdir(parents=True)
            trusted_ir = trusted_root / "Factory.wav"
            trusted_ir.write_bytes(b"trusted-wave")
            outside_ir = base / "outside.wav"
            outside_ir.write_bytes(b"outside-wave")
            os.symlink(trusted_ir, cab_root / "Factory.wav")
            os.symlink(outside_ir, cab_root / "Outside.wav")

            warnings = []
            result = inspect.scan_assets(
                data_root,
                warnings,
                throttle_mib_per_sec=0,
                trusted_symlink_roots={"cab_ir": (trusted_root,)},
            )

            self.assertEqual(result["asset_count"], 1)
            self.assertEqual(result["items"][0]["relative_path"], "CabIR/Factory.wav")
            self.assertEqual(
                [item["code"] for item in warnings], ["SYMLINK_TARGET_NOT_ALLOWED"]
            )


class CatalogDigestTests(unittest.TestCase):
    def test_digest_ignores_asset_mtime_and_absolute_root(self):
        plugin = inspect.parse_lv2info(LV2INFO_SAMPLE, "http://example.test/plugins/gain")
        plugin["plugin_id"] = inspect.stable_id("plg", plugin["uri"])
        plugin["descriptor_scope"] = "lv2info-v1"
        plugin["descriptor_sha256"] = inspect.sha256_json(inspect.plugin_descriptor(plugin))
        plugin["source_sha256"] = "0" * 64

        lv2 = {"plugins": [plugin]}
        base_asset = {
            "asset_id": "ast_" + "1" * 24,
            "kind": "nam",
            "relative_path": "NeuralAmpModels/Test.nam",
            "extension": ".nam",
            "size_bytes": 12,
            "mtime_ns": 1,
            "sha256": "2" * 64,
        }
        first = {"upload_root": "/one", "items": [dict(base_asset)]}
        second_asset = dict(base_asset)
        second_asset["mtime_ns"] = 999
        second = {"upload_root": "/another", "items": [second_asset]}

        first_digest = inspect.sha256_json(inspect.build_catalog_projection(lv2, first))
        second_digest = inspect.sha256_json(inspect.build_catalog_projection(lv2, second))
        self.assertEqual(first_digest, second_digest)


class AtomicWriteTests(unittest.TestCase):
    def test_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            inspect.atomic_write_json(path, {"ok": True}, force=False)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                inspect.atomic_write_json(path, {"ok": False}, force=False)


if __name__ == "__main__":
    unittest.main()
