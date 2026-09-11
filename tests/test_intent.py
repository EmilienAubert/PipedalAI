from __future__ import annotations

import unittest

from pydantic import ValidationError

from pipedal_ai.intent import (
    ChainConstraints,
    DelayIntent,
    SpaceIntent,
    ToneIntent,
    fallback_tone_intent,
)


class ToneIntentContractTests(unittest.TestCase):
    def test_minimal_contract_is_versioned_and_json_round_trips(self):
        intent = ToneIntent(prompt="Son clean et dynamique")
        self.assertEqual(intent.schema_version, "pipedal-ai.tone-intent/1.0.0")
        self.assertEqual(ToneIntent.model_validate_json(intent.model_dump_json()), intent)

    def test_extra_fields_are_forbidden_at_every_level(self):
        with self.assertRaises(ValidationError):
            ToneIntent.model_validate({"prompt": "Son clean", "invented": True})
        with self.assertRaises(ValidationError):
            ToneIntent.model_validate({"prompt": "Son clean", "gain": {"amount": 0.2, "invented": True}})

    def test_wrong_version_and_out_of_bounds_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            ToneIntent.model_validate({"schema_version": "pipedal-ai.tone-intent/2.0.0", "prompt": "Son clean"})
        with self.assertRaises(ValidationError):
            ToneIntent.model_validate({"prompt": "Son clean", "gain": {"amount": 1.01}})
        with self.assertRaises(ValidationError):
            ToneIntent.model_validate({"prompt": "Son clean", "spectrum": {"treble": -1.01}})
        with self.assertRaises(ValidationError):
            ToneIntent.model_validate({"prompt": "   "})

    def test_effect_state_and_chain_constraints_are_coherent(self):
        with self.assertRaises(ValidationError):
            SpaceIntent(enabled=False, kind="room", mix=0.2)
        with self.assertRaises(ValidationError):
            DelayIntent(enabled=True, kind="tape", mix=0.2)
        with self.assertRaises(ValidationError):
            ChainConstraints(required_roles=["delay"], forbidden_roles=["delay"])

    def test_fallback_extracts_french_tone_and_profile(self):
        prompt = (
            "Blues chaud, légèrement crunch, très dynamique, graves fermes, "
            "médiums présents, aigus doux et une petite ambiance de pièce"
        )
        profile = {
            "profile_id": "tele-1",
            "guitar": "Telecaster chevalet",
            "pickup": "single_coil",
            "input_trim_db": -2,
        }
        intent = fallback_tone_intent(prompt, profile)
        self.assertEqual(intent.gain.character, "crunch")
        self.assertGreater(intent.dynamics.pick_sensitivity, 0.9)
        self.assertGreater(intent.spectrum.bass_tightness, 0.8)
        self.assertGreater(intent.spectrum.mids, 0.0)
        self.assertLess(intent.spectrum.treble, 0.0)
        self.assertEqual(intent.space.kind, "room")
        self.assertLessEqual(intent.space.mix, 0.12)
        self.assertEqual(intent.style.genres, ["blues"])
        self.assertEqual(intent.guitar.pickup, "single_coil")
        self.assertEqual(intent.guitar.output_level, "low")
        self.assertEqual(intent.guitar.input_trim_db, -2.0)

    def test_explicit_dry_and_no_delay_override_effect_words(self):
        intent = fallback_tone_intent("Rock avec reverb et delay, mais sec et sans delay")
        self.assertFalse(intent.space.enabled)
        self.assertFalse(intent.delay.enabled)
        self.assertIn("reverb", intent.chain_constraints.forbidden_roles)
        self.assertIn("delay", intent.chain_constraints.forbidden_roles)

    def test_fallback_is_deterministic_and_handles_modulation_delay_and_era(self):
        prompt = "Rock vintage années 1970, chorus léger et tape delay"
        first = fallback_tone_intent(prompt)
        second = fallback_tone_intent(prompt)
        self.assertEqual(first, second)
        self.assertEqual(first.style.era, "1970s")
        self.assertEqual(first.modulation.kind, "chorus")
        self.assertEqual(first.delay.kind, "tape")

    def test_light_saturation_maps_to_edge_of_breakup(self):
        intent = fallback_tone_intent("Son légèrement saturé et très réactif")
        self.assertEqual(intent.gain.character, "edge_of_breakup")


if __name__ == "__main__":
    unittest.main()
