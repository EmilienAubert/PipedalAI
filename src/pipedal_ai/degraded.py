from __future__ import annotations

import re
from typing import Any

from .models import CatalogRef, ChainStep, PresetSpec, ProposalSet, ResourceBinding


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _find_plugin(capabilities: dict[str, Any], *needles: str) -> dict[str, Any] | None:
    lowered = tuple(needle.lower() for needle in needles)
    for plugin in capabilities["plugins"]:
        text = f"{plugin.get('name', '')} {plugin.get('uri', '')}".lower()
        if plugin.get("allowed_in_live_chain", True) and any(needle in text for needle in lowered):
            return plugin
    return None


def _choose_asset(capabilities: dict[str, Any], kind: str, prompt: str) -> dict[str, Any] | None:
    candidates = [asset for asset in capabilities["assets"] if asset["kind"] == kind]
    if not candidates:
        return None
    tokens = _words(prompt)
    tone_tokens = tokens & {
        "clean", "crunch", "metal", "lead", "rock", "blues", "vox", "marshall",
        "fender", "mesa", "orange", "5150", "jcm", "ac30", "bright", "warm",
    }
    def score(asset: dict[str, Any]) -> tuple[int, str]:
        asset_words = _words(asset["display_name"])
        return (-len(tone_tokens & asset_words), asset["display_name"].lower())
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
        lower = prompt.lower()
        input_stage = _find_plugin(capabilities, "toob input stage")
        nam = _find_plugin(capabilities, "neural amp modeler", "toob-nam")
        eq = _find_plugin(capabilities, "parametric eq (mono)", "3 band eq (mono)")
        volume = _find_plugin(capabilities, "toob volume")
        ambience = _find_plugin(capabilities, "toob freeverb")
        drive = None
        if any(word in lower for word in ("metal", "agress", "high gain", "satur")):
            drive = _find_plugin(capabilities, "gxrat", "tube screamer", "gxsd1")
        elif any(word in lower for word in ("blues", "crunch", "rock", "overdrive")):
            drive = _find_plugin(capabilities, "tube screamer", "gxsd1", "clubdrive")

        nam_asset = _choose_asset(capabilities, "nam", prompt) if nam else None
        if nam and not nam_asset:
            nam = None
        wants_space = any(word in lower for word in ("reverb", "ambi", "spacious", "room", "hall"))
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
                            {"trim": values["trim"] + profile_trim, "gate_t": values["gate"], "locut": 60, "hicut": 12500},
                        ),
                    )
                )
            if drive and values["drive"]:
                chain.append(ChainStep(instance_id="drive", plugin_id=drive["plugin_id"], bypass=False))
            if nam:
                resources = []
                if nam_asset:
                    resources.append(ResourceBinding(role="nam_model", asset_id=nam_asset["asset_id"]))
                chain.append(
                    ChainStep(
                        instance_id="amp",
                        plugin_id=nam["plugin_id"],
                        bypass=False,
                        parameters=_safe_parameters(
                            nam,
                            {"inputGain": 0, "outputGain": 0, "gate": values["gate"], "buffer": True, "calibration": -6},
                        ),
                        resources=resources,
                    )
                )
            if eq:
                chain.append(ChainStep(instance_id="eq", plugin_id=eq["plugin_id"], bypass=False))
            if ambience and wants_space:
                chain.append(
                    ChainStep(
                        instance_id="space",
                        plugin_id=ambience["plugin_id"],
                        bypass=False,
                        parameters=_safe_parameters(ambience, {"bypass": True, "dryWet": values["wet"], "roomSize": 0.45, "damping": 0.35, "tails": True}),
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
            variants.append(
                PresetSpec(
                    schema_version="pipedal-ai.preset-spec/1.0.0",
                    catalog=catalog,
                    variant=variant,
                    name=f"AI {variant.capitalize()} — {prompt[:40]}",
                    description="Recette locale déterministe créée sans la RTX.",
                    chain=chain,
                )
            )
        return ProposalSet(
            schema_version="pipedal-ai.proposal-set/1.0.0",
            request_id=request_id,
            catalog=catalog,
            proposals=variants,
        )
