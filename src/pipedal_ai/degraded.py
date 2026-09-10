from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import CatalogRef, ChainStep, PresetSpec, ProposalSet, ResourceBinding


TAG_ALIASES = {
    "clean": {"clean", "clair", "claire", "propre", "cristallin", "cristalline"},
    "crunch": {
        "crunch", "breakup", "saturation", "sature", "saturee",
        "legerement sature", "legerement saturee",
    },
    "high_gain": {
        "high gain", "gros gain", "metal", "agressif", "agressive",
        "grosse saturation", "fortement sature", "fortement saturee",
    },
    "warm": {"warm", "chaud", "chaude", "rond", "ronde", "doux", "douce"},
    "bright": {"bright", "brillant", "brillante", "cristallin", "cristalline"},
    "dark": {"dark", "sombre", "feutre", "feutree"},
    "space": {
        "reverb", "reverberation", "ambiance", "ambiant", "ambiante", "spacious",
        "room", "hall", "piece",
    },
    "dry": {"dry", "sec", "seche", "sans reverb", "sans reverberation", "sans ambiance"},
}

BRAND_TAGS = {"vox", "marshall", "fender", "mesa", "orange", "5150", "jcm", "ac30", "tweed"}
GAIN_TAGS = {"clean", "crunch", "high_gain", "lead", "rock", "blues"}
TONE_TAGS = {"warm", "bright", "dark"}


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", plain.lower()))


def _words(value: str) -> set[str]:
    return set(_normalize(value).split())


def _contains_phrase(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def _prompt_tags(prompt: str) -> set[str]:
    normalized = _normalize(prompt)
    tags = set(normalized.split())
    for canonical, aliases in TAG_ALIASES.items():
        if any(_contains_phrase(normalized, _normalize(alias)) for alias in aliases):
            tags.add(canonical)
    return tags


def _find_plugin(capabilities: dict[str, Any], *needles: str) -> dict[str, Any] | None:
    """Return the first installed plugin according to caller priority."""
    plugins = [
        plugin for plugin in capabilities["plugins"]
        if plugin.get("allowed_in_live_chain", True)
    ]
    for needle in needles:
        normalized_needle = _normalize(needle)
        for plugin in plugins:
            text = _normalize(f"{plugin.get('name', '')} {plugin.get('uri', '')}")
            if normalized_needle in text:
                return plugin
    return None


def _choose_asset(
    capabilities: dict[str, Any], kind: str, prompt: str,
) -> dict[str, Any] | None:
    candidates = [asset for asset in capabilities["assets"] if asset["kind"] == kind]
    if not candidates:
        return None

    tags = _prompt_tags(prompt)
    requested = tags & (BRAND_TAGS | GAIN_TAGS | TONE_TAGS)
    if not requested:
        requested = {"clean"}

    def score(asset: dict[str, Any]) -> tuple[int, str, str]:
        asset_words = _words(asset["display_name"])
        value = 0
        value += 8 * len((requested & BRAND_TAGS) & asset_words)
        value += 5 * len((requested & GAIN_TAGS) & asset_words)
        value += 2 * len((requested & TONE_TAGS) & asset_words)
        if "high_gain" in requested and asset_words & {"5150", "metal", "lead", "red", "high", "gain"}:
            value += 5
        if "clean" in requested and asset_words & {"crunch", "cranked", "lead", "metal", "red"}:
            value -= 6
        if "high_gain" in requested and "clean" in asset_words:
            value -= 6
        return (-value, asset["display_name"].lower(), asset["asset_id"])

    return sorted(candidates, key=score)[0]


def _safe_parameters(plugin: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    controls = {control["symbol"]: control for control in plugin.get("controls", [])}
    result: dict[str, Any] = {}
    for key, value in requested.items():
        control = controls.get(key)
        if control is None:
            continue
        datatype = control.get("datatype")
        if datatype == "boolean":
            result[key] = bool(value)
            continue
        minimum, maximum = control.get("minimum"), control.get("maximum")
        numeric = value
        if minimum is not None:
            numeric = max(minimum, numeric)
        if maximum is not None:
            numeric = min(maximum, numeric)
        result[key] = int(round(numeric)) if datatype in {"integer", "enumeration"} else numeric
    return result


def _drive_parameters(plugin: dict[str, Any], variant: str, high_gain: bool) -> dict[str, Any]:
    amount = {"balanced": 0.22, "bold": 0.38}[variant] + (0.12 if high_gain else 0)
    text = _normalize(f"{plugin.get('name', '')} {plugin.get('uri', '')}")
    if "gxrat" in text or "aclipper" in text:
        requested = {"DRIVE": amount, "LEVEL": -9, "TONE": 0.48, "BYPASS": True}
    elif "gxtubescreamer" in text or "gxts9" in text:
        requested = {"fslider0_": -10, "fslider1_": 420, "fslider2_": amount, "BYPASS": True}
    elif "gxsd1" in text or "sd1sim" in text:
        requested = {"DRIVE": amount, "LEVEL": -10, "TONE": 420, "BYPASS": True}
    else:
        requested = {"DRIVE": amount, "VOLUME": 0.45, "BYPASS": True}
    return _safe_parameters(plugin, requested)


def _eq_parameters(plugin: dict[str, Any], tags: set[str], variant: str) -> dict[str, Any]:
    intensity = {"conservative": 0.5, "balanced": 0.8, "bold": 1.2}[variant]
    bass = mid = treble = 5.0
    low = low_mid = high_mid = high = 0.0
    if "warm" in tags:
        bass += intensity
        mid += intensity * 0.5
        treble -= intensity
        low, low_mid, high_mid, high = intensity, intensity * 0.5, -intensity * 0.4, -intensity
    elif "bright" in tags:
        bass -= intensity * 0.4
        treble += intensity
        low, high_mid, high = -intensity * 0.4, intensity * 0.5, intensity
    elif "dark" in tags:
        treble -= intensity * 1.5
        high_mid, high = -intensity * 0.8, -intensity * 1.5
    return _safe_parameters(
        plugin,
        {
            "bass": bass, "mid": mid, "treble": treble, "gain": 0,
            "lfLevel": low, "lmfLevel": low_mid, "hmfLevel": high_mid, "hfLevel": high,
        },
    )


class DegradedProposer:
    """Deterministic, catalog-only fallback used when the RTX is unavailable."""

    def propose(
        self,
        request_id: str,
        prompt: str,
        capabilities: dict[str, Any],
        profile: dict[str, Any] | None = None,
    ) -> ProposalSet:
        catalog = CatalogRef.model_validate(capabilities["catalog"])
        tags = _prompt_tags(prompt)
        high_gain = "high_gain" in tags
        crunchy = high_gain or bool(tags & {"blues", "crunch", "rock", "overdrive"})
        wants_space = "space" in tags and "dry" not in tags

        input_stage = _find_plugin(capabilities, "toob input stage")
        nam = _find_plugin(capabilities, "neural amp modeler", "toob nam")
        eq = _find_plugin(capabilities, "parametric eq mono", "3 band eq mono")
        volume = _find_plugin(capabilities, "toob volume")
        ambience = _find_plugin(capabilities, "toob freeverb")
        drive = None
        if high_gain:
            drive = _find_plugin(capabilities, "gxrat", "gxsd1", "gxtubescreamer", "clubdrive")
        elif crunchy:
            drive = _find_plugin(capabilities, "gxtubescreamer", "gxsd1", "clubdrive", "gxrat")

        nam_asset = _choose_asset(capabilities, "nam", prompt) if nam else None
        if nam and not nam_asset:
            nam = None

        variants = []
        settings = {
            "conservative": {"drive": False, "trim": -9, "gate": -90, "out": -9, "wet": 0.10},
            "balanced": {"drive": True, "trim": -6, "gate": -82, "out": -7, "wet": 0.18},
            "bold": {"drive": True, "trim": -4, "gate": -75, "out": -8, "wet": 0.28},
        }
        for variant in ("conservative", "balanced", "bold"):
            values = settings[variant]
            chain: list[ChainStep] = []
            if input_stage:
                profile_trim = float(profile.get("input_trim_db", 0)) if profile else 0
                chain.append(
                    ChainStep(
                        instance_id="input",
                        plugin_id=input_stage["plugin_id"],
                        bypass=False,
                        parameters=_safe_parameters(
                            input_stage,
                            {
                                "trim": values["trim"] + profile_trim,
                                "gate_t": values["gate"],
                                "locut": 60,
                                "hicut": 12500,
                                "bright": 2 if "bright" in tags else 0,
                            },
                        ),
                    )
                )
            if drive and values["drive"]:
                chain.append(
                    ChainStep(
                        instance_id="drive",
                        plugin_id=drive["plugin_id"],
                        bypass=False,
                        parameters=_drive_parameters(drive, variant, high_gain),
                    )
                )
            if nam:
                resources = [ResourceBinding(role="nam_model", asset_id=nam_asset["asset_id"])]
                chain.append(
                    ChainStep(
                        instance_id="amp",
                        plugin_id=nam["plugin_id"],
                        bypass=False,
                        parameters=_safe_parameters(
                            nam,
                            {
                                "inputGain": -2 if high_gain else 0,
                                "outputGain": 0,
                                "gate": values["gate"],
                                "buffer": True,
                                "calibration": -6,
                                "bass": 5.5 if "warm" in tags else 5,
                                "mid": 5.5 if "warm" in tags else 5,
                                "treble": 5.5 if "bright" in tags else (4.5 if tags & {"warm", "dark"} else 5),
                            },
                        ),
                        resources=resources,
                    )
                )
            if eq:
                chain.append(
                    ChainStep(
                        instance_id="eq",
                        plugin_id=eq["plugin_id"],
                        bypass=False,
                        parameters=_eq_parameters(eq, tags, variant),
                    )
                )
            if ambience and wants_space:
                chain.append(
                    ChainStep(
                        instance_id="space",
                        plugin_id=ambience["plugin_id"],
                        bypass=False,
                        parameters=_safe_parameters(
                            ambience,
                            {"bypass": True, "dryWet": values["wet"], "roomSize": 0.45, "damping": 0.35, "tails": True},
                        ),
                    )
                )
            if volume:
                chain.append(
                    ChainStep(
                        instance_id="output",
                        plugin_id=volume["plugin_id"],
                        bypass=False,
                        parameters=_safe_parameters(volume, {"vol": values["out"]}),
                    )
                )
            if not chain:
                first = next(plugin for plugin in capabilities["plugins"] if plugin.get("allowed_in_live_chain", True))
                chain.append(ChainStep(instance_id="effect", plugin_id=first["plugin_id"], bypass=False))

            choices = []
            if drive and values["drive"]:
                choices.append(f"drive={drive['name']}")
            if nam_asset:
                choices.append(f"NAM={nam_asset['display_name']}")
            choices.append(f"ambiance={'oui' if ambience and wants_space else 'non'}")
            description = f"Recette locale déterministe : {', '.join(choices)}."
            variants.append(
                PresetSpec(
                    schema_version="pipedal-ai.preset-spec/1.0.0",
                    catalog=catalog,
                    variant=variant,
                    name=f"AI {variant.capitalize()} — {prompt[:40]}",
                    description=description[:500],
                    chain=chain,
                )
            )
        return ProposalSet(
            schema_version="pipedal-ai.proposal-set/1.0.0",
            request_id=request_id,
            catalog=catalog,
            proposals=variants,
        )
