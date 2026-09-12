import copy
import hashlib
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inspect = load_module("pipedal_ai_inspect", ROOT / "pipedal_ai_inspect.py")
catalog = load_module("pipedal_ai_catalog", ROOT / "pipedal_ai_catalog.py")


LV2INFO = """http://example.test/gain

\tName:              Test Gain
\tClass:             Amplifier Plugin
\tAuthor:            Tests
\tHas latency:       no

\tPort 0:
\t\tType:        http://lv2plug.in/ns/lv2core#InputPort
\t\t             http://lv2plug.in/ns/lv2core#ControlPort
\t\tSymbol:      gain
\t\tName:        Gain
\t\tMinimum:     0
\t\tMaximum:     1
\t\tDefault:     0.5
"""


def make_inventory():
    plugin = inspect.parse_lv2info(LV2INFO, "http://example.test/gain")
    plugin["plugin_id"] = inspect.stable_id("plg", plugin["uri"])
    plugin["descriptor_scope"] = "lv2info-v1"
    plugin["descriptor_sha256"] = inspect.sha256_json(inspect.plugin_descriptor(plugin))
    plugin["source_sha256"] = hashlib.sha256(LV2INFO.encode()).hexdigest()
    asset_hash = hashlib.sha256(b"nam-data").hexdigest()
    asset = {
        "asset_id": inspect.stable_id(
            "ast", "nam", "NeuralAmpModels/Test.nam", asset_hash
        ),
        "kind": "nam",
        "relative_path": "NeuralAmpModels/Test.nam",
        "extension": ".nam",
        "size_bytes": 8,
        "mtime_ns": 1,
        "sha256": asset_hash,
    }
    lv2 = {"status": "complete", "tools": {}, "plugin_count": 1, "plugins": [plugin]}
    assets = {
        "upload_root": "/var/pipedal/audio_uploads",
        "roots": [],
        "asset_count": 1,
        "items": [asset],
    }
    inventory = {
        "schema_version": inspect.SCHEMA_VERSION,
        "generated_at": "2026-09-10T00:00:00Z",
        "status": "complete",
        "collector": {
            "name": "pipedal-ai-inspect",
            "version": "0.2.0",
            "network_access": False,
            "shell_execution": False,
        },
        "host": {},
        "pipedal": {},
        "lv2": lv2,
        "assets": assets,
        "catalog": {
            "schema_version": inspect.CATALOG_SCHEMA_VERSION,
            "version": None,
            "sha256": inspect.sha256_json(inspect.build_catalog_projection(lv2, assets)),
            "plugin_count": 1,
            "asset_count": 1,
        },
        "warnings": [],
    }
    return inventory


class CatalogValidationTests(unittest.TestCase):
    def test_rejects_tampered_catalog_hash(self):
        inventory = make_inventory()
        inventory["catalog"]["sha256"] = "0" * 64
        with self.assertRaises(catalog.InventoryValidationError):
            catalog.validate_inventory(inventory)

    def test_rejects_parent_path_even_with_recomputed_hashes(self):
        inventory = make_inventory()
        asset = inventory["assets"]["items"][0]
        asset["relative_path"] = "NeuralAmpModels/../escape.nam"
        asset["asset_id"] = inspect.stable_id(
            "ast", asset["kind"], asset["relative_path"], asset["sha256"]
        )
        inventory["catalog"]["sha256"] = inspect.sha256_json(
            inspect.build_catalog_projection(inventory["lv2"], inventory["assets"])
        )
        with self.assertRaises(catalog.InventoryValidationError):
            catalog.validate_inventory(inventory)


class CatalogDatabaseTests(unittest.TestCase):
    def test_refuses_newer_database_schema_without_downgrading_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.db"
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA user_version = 99")
            connection.close()
            with self.assertRaises(catalog.InventoryValidationError):
                catalog.open_database(database)
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
            finally:
                connection.close()

    def test_import_into_known_application_versions_preserves_app_data_and_version(self):
        for version in (3, 4):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                database = Path(temporary) / "catalog.db"
                connection = catalog.open_database(database)
                connection.execute("CREATE TABLE application_marker(value TEXT)")
                connection.execute("INSERT INTO application_marker VALUES('preserve me')")
                connection.execute(f"PRAGMA user_version={version}")
                connection.close()
                connection = catalog.open_database(database)
                try:
                    catalog.import_inventory(connection, make_inventory(), "1" * 64)
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)
                    self.assertEqual(connection.execute("SELECT value FROM application_marker").fetchone()[0],
                                     "preserve me")
                finally:
                    connection.close()

    def test_imports_very_large_lv2_numeric_sentinel_as_real(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = catalog.open_database(Path(temporary) / "catalog.db")
            try:
                inventory = make_inventory()
                plugin = inventory["lv2"]["plugins"][0]
                plugin["ports"][0]["maximum"] = 10**38
                plugin["descriptor_sha256"] = inspect.sha256_json(
                    inspect.plugin_descriptor(plugin)
                )
                inventory["catalog"]["sha256"] = inspect.sha256_json(
                    inspect.build_catalog_projection(inventory["lv2"], inventory["assets"])
                )
                catalog.import_inventory(connection, inventory, "3" * 64)
                stored = connection.execute(
                    "SELECT maximum FROM plugin_ports"
                ).fetchone()[0]
                self.assertIsInstance(stored, float)
                self.assertEqual(stored, 1e38)
            finally:
                connection.close()

    def test_import_is_immutable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "catalog.db"
            connection = catalog.open_database(database)
            try:
                inventory = make_inventory()
                first = catalog.import_inventory(connection, inventory, "1" * 64)
                second = catalog.import_inventory(connection, inventory, "1" * 64)
                self.assertTrue(first["created"])
                self.assertFalse(second["created"])
                self.assertEqual(first["revision"], 1)
                self.assertEqual(second["revision"], 1)
                self.assertEqual(catalog.database_status(connection)["revision_count"], 1)
                self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            finally:
                connection.close()

    def test_new_hash_creates_and_activates_next_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection = catalog.open_database(Path(temporary) / "catalog.db")
            try:
                first = make_inventory()
                catalog.import_inventory(connection, first, "1" * 64)
                second = copy.deepcopy(first)
                plugin = second["lv2"]["plugins"][0]
                plugin["name"] = "Updated Gain"
                plugin["descriptor_sha256"] = inspect.sha256_json(
                    inspect.plugin_descriptor(plugin)
                )
                second["catalog"]["sha256"] = inspect.sha256_json(
                    inspect.build_catalog_projection(second["lv2"], second["assets"])
                )
                result = catalog.import_inventory(connection, second, "2" * 64)
                status = catalog.database_status(connection)
                self.assertEqual(result["revision"], 2)
                self.assertEqual(status["revision_count"], 2)
                self.assertEqual(status["active"]["revision"], 2)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
