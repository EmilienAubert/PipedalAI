import copy
import importlib.util
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


catalog_tests = load_module("catalog_test_helpers", ROOT / "tests" / "test_catalog.py")
catalog = catalog_tests.catalog
contracts = load_module("pipedal_ai_contracts", ROOT / "pipedal_ai_contracts.py")


def make_database(directory):
    connection = catalog.open_database(Path(directory) / "catalog.db")
    inventory = catalog_tests.make_inventory()
    catalog.import_inventory(connection, inventory, "1" * 64)
    return connection, inventory


def make_spec(inventory, variant="balanced"):
    plugin = inventory["lv2"]["plugins"][0]
    return {
        "schema_version": contracts.PRESET_SCHEMA_VERSION,
        "catalog": {
            "revision": 1,
            "sha256": inventory["catalog"]["sha256"],
        },
        "variant": variant,
        "name": "Test preset",
        "description": "Test",
        "chain": [
            {
                "instance_id": "gain",
                "plugin_id": plugin["plugin_id"],
                "bypass": False,
                "parameters": {"gain": 0.75},
                "resources": [],
            }
        ],
    }


class CapabilityTests(unittest.TestCase):
    def test_export_contains_no_local_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, _ = make_database(temporary)
            try:
                result = contracts.build_capability_set(connection)
                rendered = str(result)
                self.assertNotIn("relative_path", rendered)
                self.assertNotIn("/var/pipedal", rendered)
                self.assertEqual(len(result["plugins"]), 1)
                self.assertEqual(len(result["assets"]), 1)
            finally:
                connection.close()


class PresetValidationTests(unittest.TestCase):
    def test_accepts_known_plugin_and_parameter(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, inventory = make_database(temporary)
            try:
                contracts.validate_preset_spec(connection, make_spec(inventory))
            finally:
                connection.close()

    def test_rejects_stale_catalog_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, inventory = make_database(temporary)
            try:
                spec = make_spec(inventory)
                spec["catalog"]["sha256"] = "0" * 64
                with self.assertRaises(contracts.ContractValidationError):
                    contracts.validate_preset_spec(connection, spec)
            finally:
                connection.close()

    def test_rejects_unknown_or_out_of_range_parameter(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, inventory = make_database(temporary)
            try:
                unknown = make_spec(inventory)
                unknown["chain"][0]["parameters"] = {"shell": 1}
                with self.assertRaises(contracts.ContractValidationError):
                    contracts.validate_preset_spec(connection, unknown)
                excessive = make_spec(inventory)
                excessive["chain"][0]["parameters"] = {"gain": 5}
                with self.assertRaises(contracts.ContractValidationError):
                    contracts.validate_preset_spec(connection, excessive)
            finally:
                connection.close()

    def test_proposal_set_requires_three_ordered_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            connection, inventory = make_database(temporary)
            try:
                catalog_ref = {"revision": 1, "sha256": inventory["catalog"]["sha256"]}
                proposal = {
                    "schema_version": contracts.PROPOSAL_SCHEMA_VERSION,
                    "request_id": "test-request",
                    "catalog": catalog_ref,
                    "proposals": [
                        make_spec(inventory, "conservative"),
                        make_spec(inventory, "balanced"),
                        make_spec(inventory, "bold"),
                    ],
                }
                contracts.validate_proposal_set(connection, proposal)
                invalid = copy.deepcopy(proposal)
                invalid["proposals"].reverse()
                with self.assertRaises(contracts.ContractValidationError):
                    contracts.validate_proposal_set(connection, invalid)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
