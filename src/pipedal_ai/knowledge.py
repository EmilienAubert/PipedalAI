"""Versioned musical knowledge attached to actual LV2 URIs, never guessed symbols."""
from __future__ import annotations

import math
from collections.abc import Mapping


KNOWLEDGE_VERSION = "pipedal-ai.plugin-knowledge/1.0.0"
TOOB = "http://two-play.com/plugins/toob-"
AXIS = "http://guitarix.sourceforge.net/plugins/gx_AxisFace_#_AxisFace_"
PROFILES = {
    TOOB + "input_stage": ("input", "input", "Trim d'entrée en dB; filtres en Hz"),
    TOOB + "noise-gate": ("noise_gate", "gate", "Seuil en dB; attaque et relâchement en ms"),
    TOOB + "nam": ("amp", "nam", "Gain en dB; calibration indépendante du son demandé"),
    TOOB + "cab-ir": ("cabinet", "cab", "IR de cabinet; gains de mélange en dB"),
    TOOB + "parametric-eq": ("eq", "peq", "Gains en dB; hiCut/hmfC/hfC en kHz"),
    TOOB + "parametric-eq-stereo": ("eq", "peq", "Gains en dB; hiCut/hmfC/hfC en kHz"),
    TOOB + "three-band-eq": ("eq", "eq", "Tonalités 0..10; gain de sortie en dB"),
    TOOB + "three-band-eq-stereo": ("eq", "eq", "Tonalités 0..10; gain de sortie en dB"),
    TOOB + "tone": ("eq", "tone", "Inclinaison -1..1; gain en dB"),
    TOOB + "tone-stereo": ("eq", "tone", "Inclinaison -1..1; gain en dB"),
    TOOB + "volume": ("output", "volume", "Atténuation de sortie en dB"),
    TOOB + "chorus": ("modulation", "chorus", "Chorus CE-2; mélange/rate/depth 0..1"),
    TOOB + "delay": ("delay", "delay", "Temps en ms; level et feedback en pourcentage"),
    TOOB + "freeverb": ("reverb", "freeverb", "Mélange, taille et amortissement 0..1"),
    TOOB + "convolution-reverb": ("reverb", "convolution", "IR de réverbération; mélange en dB"),
    TOOB + "convolution-reverb-stereo": ("reverb", "convolution", "IR de réverbération; mélange en dB"),
    TOOB + "phaser": ("modulation", "phaser", "Vitesse en Hz; mélange 0..1"),
    TOOB + "flanger": ("modulation", "flanger", "Mélange/profondeur/vitesse 0..1"),
    TOOB + "flanger-stereo": ("modulation", "flanger", "Mélange/profondeur/vitesse 0..1"),
    TOOB + "tremolo": ("modulation", "tremolo", "Vitesse en Hz; profondeur 0..1"),
    TOOB + "tremolo-mono": ("modulation", "tremolo", "Vitesse en Hz; profondeur 0..1"),
    AXIS: ("drive", "fuzz", "Fuzz Axis Face; jamais un compresseur; avant l'ampli"),
}
ORDER = {"input": 0, "noise_gate": 1, "compressor": 2, "pitch": 3, "wah": 4,
         "drive": 5, "amp": 6, "cabinet": 7, "eq": 8, "modulation": 9,
         "delay": 10, "reverb": 11, "output": 12}


def plugin_knowledge(plugin: Mapping) -> dict:
    profile = PROFILES.get(plugin.get("uri"))
    if profile:
        role, adapter, description = profile
        return {"version": KNOWLEDGE_VERSION, "role": role, "adapter": adapter,
                "description": description, "provenance": "curated-uri", "confidence": "verified"}
    category = str(plugin.get("class", "")).casefold()
    for word, role in (("compressor", "compressor"), ("distortion", "drive"),
                       ("amplifier", "amp"), ("reverb", "reverb"), ("delay", "delay"),
                       ("chorus", "modulation"), ("flanger", "modulation"), ("eq", "eq")):
        if word in category:
            return {"version": KNOWLEDGE_VERSION, "role": role, "adapter": None,
                    "provenance": "lv2-class", "confidence": "category-only"}
    return {"version": KNOWLEDGE_VERSION, "role": None, "adapter": None,
            "provenance": "unknown", "confidence": "unknown"}


def permits_extra_drive(intent, prompt: str) -> bool:
    import re
    text = prompt.casefold()
    return (intent.gain.character == "fuzz" or "drive" in intent.chain_constraints.required_roles
            or bool(re.search(r"\b(fuzz|pedal|pédale|tubescreamer|screamer|overdrive pedal|axisface)\b", text)))


def adapt_parameters(plugin: Mapping, intent, variant: str, profile: Mapping | None = None) -> dict:
    """Only documented mappings. Catalogue types/bounds are checked again by the Pi."""
    adapter = plugin_knowledge(plugin)["adapter"]
    change = {"conservative": -1.0, "balanced": 0.0, "bold": 1.0}[variant]
    spectrum = intent.spectrum
    treble = max(-1.0, min(1.0, spectrum.treble + 0.12 * change))
    bass = max(-1.0, min(1.0, spectrum.bass - 0.08 * change))
    def db_mix(mix):
        return max(-40.0, 20 * math.log10(max(0.01, min(0.45, mix))))
    if adapter == "input":
        values = {"trim": float((profile or {}).get("input_trim_db", 0)), "bright": 0.0,
                  "locut": 30.0, "hicut": 13000.0, "gate_t": -120.0}
    elif adapter == "gate":
        values = {"threshold": -85.0, "attack": 1.0, "hold": 60.0, "release": 180.0}
    elif adapter == "nam":
        # Tone stack and calibration stay at host defaults. NAM selection creates
        # the base character; a small trim changes drive, not a fabricated amp knob.
        values = {"inputGain": 1.25 * change, "outputGain": -max(0.0, change), "toneStack": 3}
        calibration = (profile or {}).get("nam_input_calibration_dbu")
        if calibration is not None:
            values.update(inputCalibrationMode=1, calibration=float(calibration))
    elif adapter == "cab":
        values = {"direct_mix": -40.0, "reverb_mix": 0.0, "reverb_mix2": -40.0,
                  "reverb_mix3": -40.0}
    elif adapter == "peq":
        values = {"loCut": 40.0 + 35.0 * spectrum.bass_tightness,
                  "hiCut": 21.0 if treble >= 0 else 12.0 + 5.0 * treble,
                  "lfLevel": 4.0 * bass, "lmfLevel": 3.0 * spectrum.low_mids,
                  "hmfLevel": 3.0 * spectrum.high_mids + 0.6 * change,
                  "hfLevel": 4.0 * treble, "gain": 0.0}
    elif adapter == "eq":
        values = {"bass": 5 + 2 * bass, "mid": 5 + 2 * spectrum.mids,
                  "treble": 5 + 2 * treble, "gain": 0.0}
    elif adapter == "tone":
        values = {"tone": treble * 0.5, "gain": 0.0}
    elif adapter == "volume":
        values = {"vol": 0.0}
    elif adapter in {"chorus", "flanger", "phaser", "tremolo"}:
        mod = intent.modulation
        values = {"rate": mod.rate if adapter != "phaser" else 0.2 + 2 * mod.rate,
                  "depth": min(0.7, mod.depth), "dryWet": min(0.35, mod.mix)}
        if adapter == "tremolo":
            values["rate"] = 0.5 + 5.0 * mod.rate
    elif adapter == "delay":
        values = {"delay": intent.delay.time_ms or 340.0, "level": 100 * min(0.35, intent.delay.mix),
                  "feedback": 100 * min(0.45, intent.delay.feedback)}
    elif adapter == "freeverb":
        values = {"dryWet": min(0.3, intent.space.mix), "roomSize": intent.space.size,
                  "damping": spectrum.warmth}
    elif adapter == "convolution":
        values = {"direct_mix": 0.0, "reverb_mix": db_mix(intent.space.mix),
                  "time": 0.5 + 4.0 * intent.space.decay}
    elif adapter == "fuzz":
        values = {"ATTACK": min(0.65, intent.gain.amount), "SMOOTH": 0.5, "VOLUME": 0.35}
    else:
        raise ValueError(f"Aucun adaptateur musical vérifié pour {plugin.get('name')}")
    controls = {c["symbol"]: c for c in plugin.get("controls", [])}
    result = {}
    for symbol, value in values.items():
        control = controls.get(symbol)
        if control is None:
            continue  # Known host versions may expose a subset, never invent it.
        low, high = control.get("minimum"), control.get("maximum")
        if low is not None:
            value = max(low, value)
        if high is not None:
            value = min(high, value)
        datatype = control.get("datatype")
        if datatype == "boolean":
            value = bool(value)
        elif datatype in {"integer", "enumeration"}:
            value = int(round(value))
            points = control.get("scale_points") or []
            if datatype == "enumeration" and points and value not in {p["value"] for p in points}:
                raise ValueError(f"Adaptateur incompatible avec l'énumération {symbol}")
        else:
            value = round(float(value), 4)
        result[symbol] = value
    return result
