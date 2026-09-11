from __future__ import annotations

import copy
import json
import unittest

from pipedal_ai.intent import fallback_tone_intent
from pipedal_ai.rtx.retrieval import CandidateRetriever, build_shortlist


def plugin(
    suffix: str,
    name: str,
    uri: str,
    *,
    roles: list[str] | None = None,
    live: bool = True,
    outputs: int = 1,
) -> dict:
    return {
        "plugin_id": f"plg_{suffix * 24}",
        "uri": uri,
        "name": name,
        "class": "Plugin",
        "descriptor_sha256": suffix * 64,
        "audio_inputs": 1,
        "audio_outputs": outputs,
        "allowed_in_live_chain": live,
        "resource_roles": roles or [],
        "controls": [],
    }


def asset(suffix: str, kind: str, name: str, **metadata) -> dict:
    return {
        "asset_id": f"ast_{suffix * 24}",
        "display_name": name,
        "kind": kind,
        "extension": ".nam" if kind == "nam" else ".wav",
        "size_bytes": 100,
        "sha256": suffix * 64,
        **metadata,
    }


class CandidateRetrieverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capabilities = {
            "schema_version": "pipedal-ai.catalog-capabilities/1.0.0",
            "catalog": {"revision": 7, "sha256": "a" * 64},
            "capability_sha256": "b" * 64,
            "plugins": [
                plugin("1", "TooB Input Stage", "http://two-play.com/plugins/toob-input_stage"),
                plugin("2", "TooB Noise Gate", "http://two-play.com/plugins/toob-noise-gate"),
                plugin("3", "TooB Parametric EQ (Mono)", "http://two-play.com/plugins/toob-parametric-eq"),
                plugin("4", "TooB Volume", "http://two-play.com/plugins/toob-volume"),
                plugin("5", "TooB Neural Amp Modeler", "http://two-play.com/plugins/toob-nam", roles=["nam_model"]),
                plugin("6", "TooB Cab IR", "http://two-play.com/plugins/toob-cab-ir", roles=["cab_ir"]),
                plugin("7", "TooB Convolution Reverb", "http://two-play.com/plugins/toob-convolution-reverb", roles=["reverb_ir"]),
                plugin("8", "GxTubeScreamer", "urn:test:tube-screamer"),
                plugin("9", "TooB File Player", "http://two-play.com/plugins/toob-player", live=False),
                plugin("a", "TooB Record Input", "http://two-play.com/plugins/toob-record-mono", live=True),
            ],
            "assets": [
                asset("1", "nam", "Generic clean", description="clean modern amplifier"),
                asset("2", "nam", "Tweed 5E3", tags=["blues", "crunch"], makes=["Fender"], description="warm dynamic vintage amp"),
                asset("3", "nam", "Anonymous capture", fingerprint={"warmth": 0.8, "brightness": 0.25, "pick_sensitivity": 0.92}),
                asset("4", "cab_ir", "Modern V30", tags=["bright", "metal"]),
                asset("5", "cab_ir", "Vintage open back", description="warm blues cabinet"),
                asset("6", "reverb_ir", "Small wooden room", tags=["room", "short"]),
                asset("7", "other", "Never expose me"),
            ],
        }

    def test_shortlist_is_json_serializable_bounded_and_keeps_catalog(self) -> None:
        intent = fallback_tone_intent("blues chaud et dynamique avec crunch")
        value = CandidateRetriever(max_plugins=8, max_assets_per_role=2).retrieve(
            intent, intent.prompt, self.capabilities
        )

        json.dumps(value, ensure_ascii=False)
        self.assertEqual(value["catalog"], self.capabilities["catalog"])
        self.assertEqual(value["capability_sha256"], "b" * 64)
        self.assertLessEqual(len(value["plugins"]), 8)
        self.assertTrue(all(count <= 2 for count in value["selection"]["asset_counts_by_role"].values()))

    def test_safe_utilities_are_kept_and_render_only_plugins_are_excluded(self) -> None:
        value = build_shortlist(
            fallback_tone_intent("son totalement neutre"),
            "son totalement neutre",
            self.capabilities,
        )
        names = {item["name"] for item in value["plugins"]}

        self.assertTrue({"TooB Input Stage", "TooB Noise Gate", "TooB Parametric EQ (Mono)", "TooB Volume"} <= names)
        self.assertNotIn("TooB File Player", names)
        self.assertNotIn("TooB Record Input", names)
        input_stage = next(item for item in value["plugins"] if item["name"] == "TooB Input Stage")
        self.assertIn("utilitaire sûr: input", input_stage["retrieval_reasons"])

    def test_nam_metadata_and_fingerprint_are_used_for_ranking(self) -> None:
        intent = fallback_tone_intent("blues chaud, dynamique et crunch")
        value = build_shortlist(intent, intent.prompt, self.capabilities)
        nam = [item for item in value["assets"] if item["resource_role"] == "nam_model"]

        names = [item["display_name"] for item in nam]
        self.assertLess(names.index("Tweed 5E3"), names.index("Generic clean"))
        tweed = next(item for item in nam if item["display_name"] == "Tweed 5E3")
        self.assertIn("blues", " ".join(tweed["retrieval_reasons"]))
        measured = next(item for item in nam if item["display_name"] == "Anonymous capture")
        self.assertTrue(any(reason.startswith("empreinte:") for reason in measured["retrieval_reasons"]))

    def test_asset_roles_follow_available_allowed_hosts(self) -> None:
        capabilities = copy.deepcopy(self.capabilities)
        capabilities["plugins"] = [
            item for item in capabilities["plugins"] if "cab_ir" not in item.get("resource_roles", [])
        ]
        value = build_shortlist(
            fallback_tone_intent("blues chaud"), "blues chaud", capabilities
        )
        roles = {item["resource_role"] for item in value["assets"]}

        self.assertNotIn("cab_ir", roles)
        self.assertTrue(roles <= {role for plugin_item in value["plugins"] for role in plugin_item["resource_roles"]})
        self.assertNotIn("other", {item["kind"] for item in value["assets"]})

    def test_result_does_not_depend_on_catalog_order(self) -> None:
        intent = fallback_tone_intent("blues chaud avec une petite room")
        first = build_shortlist(intent, intent.prompt, self.capabilities)
        reversed_capabilities = copy.deepcopy(self.capabilities)
        reversed_capabilities["plugins"].reverse()
        reversed_capabilities["assets"].reverse()
        second = build_shortlist(intent, intent.prompt, reversed_capabilities)

        self.assertEqual(first, second)

    def test_tone_intent_constraints_filter_roles_and_promote_required_ones(self) -> None:
        intent = fallback_tone_intent("son clair et sec")
        constraints = intent.chain_constraints.model_copy(
            update={
                "allow_cab_ir": False,
                "required_roles": ["drive"],
                "forbidden_roles": ["reverb"],
            }
        )
        intent = intent.model_copy(update={"chain_constraints": constraints})
        value = CandidateRetriever(max_plugins=8).retrieve(intent, intent.prompt, self.capabilities)
        names = {item["name"] for item in value["plugins"]}
        roles = {item["resource_role"] for item in value["assets"]}

        self.assertIn("GxTubeScreamer", names)
        self.assertNotIn("TooB Cab IR", names)
        self.assertNotIn("TooB Convolution Reverb", names)
        self.assertNotIn("cab_ir", roles)
        self.assertNotIn("reverb_ir", roles)

    def test_nested_tone3000_metadata_influences_asset_ranking(self) -> None:
        capabilities = copy.deepcopy(self.capabilities)
        intent = fallback_tone_intent("Fender blues chaud et dynamique")
        before = build_shortlist(intent, intent.prompt, capabilities)
        before_names = [
            item["display_name"] for item in before["assets"] if item["resource_role"] == "nam_model"
        ]
        capabilities["assets"][0]["metadata"] = {
            "tone3000": {
                "title": "Warm blues deluxe",
                "description": "dynamic vintage crunch",
                "tags": ["blues", "warm"],
                "makes": ["Fender"],
                "gear": "amp",
                "model_name": "5E3 crunch",
                "model_size": "standard",
                "architecture": "2",
            }
        }
        result = build_shortlist(intent, intent.prompt, capabilities)
        nam = [item for item in result["assets"] if item["resource_role"] == "nam_model"]
        names = [item["display_name"] for item in nam]
        self.assertLess(names.index("Generic clean"), before_names.index("Generic clean"))


if __name__ == "__main__":
    unittest.main()
