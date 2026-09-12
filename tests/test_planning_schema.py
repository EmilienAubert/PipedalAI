from copy import deepcopy
import unittest

from jsonschema import Draft202012Validator

from pipedal_ai.intent import fallback_tone_intent
from pipedal_ai.models import PlanDraft
from pipedal_ai.rtx.ollama import OllamaClient, _prompt_payload
from pipedal_ai.rtx.ollama_schema import control_schema, planning_schema
from test_ollama_pipeline import capabilities, draft, request


def payload():
    plugins = []
    for index, (name, roles) in enumerate([
        ("GxSupersonic", []), ("GxUVox720k", []), ("TooB Cab Simulator", []),
        ("TooB Neural Amp Modeler", ["nam_model"]), ("TooB Cab IR", ["cab_ir"]),
    ], 1):
        plugin = deepcopy(capabilities()["plugins"][0])
        plugin.update(plugin_id=f"plg_{index:024x}", name=name, resource_roles=roles,
                      uri=f"https://example.test/plugin/{index}")
        plugin["controls"] = [{"symbol": "GAIN", "datatype": "number", "minimum": 0,
                               "maximum": 1, "default": 0.5}]
        plugins.append(plugin)
    return {
        "request_id": "job_test", "catalog": capabilities()["catalog"],
        "tone_intent": fallback_tone_intent("blues chaud").model_dump(mode="json"),
        "candidate_shortlist": {"plugins": plugins, "assets": [
            {"asset_id": "ast_" + "a" * 24, "resource_role": "nam_model", "capture_type": "amp"},
            {"asset_id": "ast_" + "b" * 24, "resource_role": "cab_ir"},
        ]},
    }


def step(index, resources=None):
    return {"instance_id": f"step_{index}", "plugin_id": f"plg_{index:024x}",
            "parameters": {"GAIN": 0.5}, "bypass": False, "resources": resources or []}


def plan(chain):
    value = draft()
    for variant in value["variants"]:
        variant["chain"] = deepcopy(chain)
    return value


class PlanningSchemaTests(unittest.TestCase):
    def setUp(self):
        self.payload = payload()
        self.schema = planning_schema(self.payload)
        Draft202012Validator.check_schema(self.schema)
        self.validator = Draft202012Validator(self.schema)
        self.nam = {"role": "nam_model", "asset_id": "ast_" + "a" * 24}
        self.ir = {"role": "cab_ir", "asset_id": "ast_" + "b" * 24}

    def test_valid_actual_controls_and_resource_hosts(self):
        self.validator.validate(plan([step(1)]))
        self.validator.validate(plan([step(4, [self.nam]), step(5, [self.ir])]))

    def test_supersonic_cannot_be_given_compressor_controls(self):
        for symbol in ("threshold", "ratio", "attack_ms", "release_ms", "gain"):
            item = step(1)
            item["parameters"] = {symbol: 0.5}
            with self.subTest(symbol=symbol):
                self.assertFalse(self.validator.is_valid(plan([item])))

    def test_amplifier_and_cab_simulator_cannot_load_files(self):
        for item in (step(2, [self.nam]), step(3, [self.ir]), step(4), step(5),
                     step(4, [self.ir]), step(5, [self.nam]), step(4, [self.nam, self.nam])):
            with self.subTest(item=item):
                self.assertFalse(self.validator.is_valid(plan([item])))

    def test_asset_id_must_match_resource_role(self):
        resource = {**self.nam, "asset_id": self.ir["asset_id"]}
        self.assertFalse(self.validator.is_valid(plan([step(4, [resource])])))

    def test_variant_order_and_chain_limit_are_in_schema(self):
        value = plan([step(1)])
        value["variants"].reverse()
        self.assertFalse(self.validator.is_valid(value))
        self.assertFalse(self.validator.is_valid(plan([step(1)] * 9)))

    def test_plugin_without_available_required_asset_is_excluded(self):
        self.payload["candidate_shortlist"]["assets"] = []
        validator = Draft202012Validator(planning_schema(self.payload))
        self.assertTrue(validator.is_valid(plan([step(1)])))
        self.assertFalse(validator.is_valid(plan([step(4, [self.nam])])))

    def test_controls_enforce_types_bounds_enums_and_exact_sentinel(self):
        cases = [
            ({"datatype": "boolean"}, [True, False], [0, 1, "true"]),
            ({"datatype": "number", "minimum": 0, "maximum": 1}, [0, 0.5, 1], [-1, 2, True]),
            ({"datatype": "integer", "minimum": 0, "maximum": 3}, [0, 3], [0.5, 4]),
            ({"datatype": "enumeration", "scale_points": [{"value": 0}, {"value": 2}]}, [0, 2], [1, True]),
            ({"datatype": "number", "minimum": 20, "maximum": 20000, "default": -1},
             [-1, 20, 20000], [-2, 0, 20001]),
        ]
        for control, valid, invalid in cases:
            validator = Draft202012Validator(control_schema(control))
            for value in valid:
                with self.subTest(control=control, value=value):
                    self.assertTrue(validator.is_valid(value))
            for value in invalid:
                with self.subTest(control=control, value=value):
                    self.assertFalse(validator.is_valid(value))

    def test_prompt_is_compact_without_mutating_validation_catalogue(self):
        self.payload["candidate_shortlist"]["assets"][0]["audio_fingerprint"] = {"large": "features"}
        before = deepcopy(self.payload)
        compact = _prompt_payload(self.payload)
        self.assertEqual(self.payload, before)
        plugin = compact["candidate_shortlist"]["plugins"][3]
        self.assertEqual(plugin["name"], "TooB Neural Amp Modeler")
        self.assertEqual(plugin["resource_roles"], ["nam_model"])
        self.assertNotIn("controls", plugin)
        self.assertNotIn("audio_fingerprint", compact["candidate_shortlist"]["assets"][0])
        self.assertEqual(compact["candidate_shortlist"]["assets"][0]["capture_type"], "amp")

    def validate_chain(self, chain):
        req = request()
        intent = fallback_tone_intent(req.prompt)
        proposal = OllamaClient._proposal_from_draft(PlanDraft.model_validate(plan(chain)), req)
        OllamaClient._validate_plan(proposal, req, intent, self.payload["candidate_shortlist"])

    def test_serial_nam_cabinet_policies(self):
        self.validate_chain([step(4, [self.nam]), step(5, [self.ir])])
        for chain in ([step(5, [self.ir]), step(4, [self.nam])],
                      [step(3), step(5, [self.ir])],
                      [step(4, [self.nam]), {**step(4, [self.nam]), "instance_id": "nam2"}]):
            with self.subTest(chain=chain), self.assertRaises(ValueError):
                self.validate_chain(chain)

    def test_unknown_capture_does_not_authorize_cabinet_or_cab_ir(self):
        self.payload["candidate_shortlist"]["assets"][0]["capture_type"] = "unknown"
        self.validate_chain([step(4, [self.nam])])
        for cabinet in (step(3), step(5, [self.ir])):
            with self.subTest(cabinet=cabinet), self.assertRaisesRegex(ValueError, "amp-only"):
                self.validate_chain([step(4, [self.nam]), cabinet])


if __name__ == "__main__":
    unittest.main()
