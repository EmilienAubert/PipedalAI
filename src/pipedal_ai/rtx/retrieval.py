from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from ..catalog import RENDER_ONLY_URIS, RESOURCE_KIND_BY_ROLE
from ..intent import ToneIntent


SHORTLIST_SCHEMA_VERSION = "pipedal-ai.candidate-shortlist/1.0.0"

# One predictable representative from each group is always kept.  They are
# inexpensive building blocks that let the planner produce a safe chain even
# when the musical prompt contains no matching plugin name.
_UTILITY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input", ("toob input stage", "input stage")),
    ("gate", ("toob noise gate", "noise gate", "gate")),
    ("equalizer", ("toob parametric eq mono", "parametric eq", "3 band eq", "graphic eq")),
    ("tone", ("toob tone mono", "tone mono", "tone stack")),
    ("output", ("toob volume", "volume")),
)

_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "clean": ("clean", "clair", "claire", "propre", "cristallin", "cristalline"),
    "crunch": ("crunch", "breakup", "overdrive", "sature", "saturee", "saturation legere"),
    "high_gain": ("high gain", "metal", "gros gain", "forte saturation", "tres sature"),
    "warm": ("warm", "chaud", "chaude", "rond", "ronde", "doux", "douce"),
    "bright": ("bright", "brillant", "brillante", "cristallin", "cristalline"),
    "dark": ("dark", "sombre", "feutre", "feutree"),
    "tight": ("tight", "ferme", "fermes", "precis", "precise", "palm mute"),
    "dynamic": ("dynamic", "dynamique", "attaque", "pick sensitivity"),
    "space": ("space", "reverb", "reverberation", "ambiance", "room", "hall", "piece"),
    "delay": ("delay", "echo", "slapback"),
    "modulation": ("chorus", "flanger", "phaser", "vibrato", "tremolo"),
}

_ROLE_ORDER = {"nam_model": 0, "cab_ir": 1, "reverb_ir": 2}


def _normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value))
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", plain.casefold()))


def _tokens(value: object) -> set[str]:
    return set(_normalize(value).split())


def _intent_payload(intent: ToneIntent) -> dict[str, Any]:
    if hasattr(intent, "model_dump"):
        value = intent.model_dump(mode="json")
    elif isinstance(intent, Mapping):  # useful for callers migrating stored v1 data
        value = dict(intent)
    else:
        raise TypeError("intent must be a ToneIntent or a mapping")
    if not isinstance(value, dict):
        raise TypeError("ToneIntent must serialize to a JSON object")
    return value


def _query_text(intent: ToneIntent, prompt: str) -> str:
    payload = _intent_payload(intent)
    parts: list[str] = [prompt, str(payload.get("prompt", ""))]
    gain = payload.get("gain", {})
    style = payload.get("style", {})
    constraints = payload.get("chain_constraints", {})
    guitar = payload.get("guitar", {})
    if isinstance(gain, Mapping):
        parts.append(str(gain.get("character", "")))
    if isinstance(style, Mapping):
        parts.append(_text_fields(style, ("genres", "era", "character")))
    if isinstance(constraints, Mapping):
        parts.append(_text_fields(constraints, ("required_roles",)))
    if isinstance(guitar, Mapping):
        parts.append(_text_fields(guitar, ("description", "pickup", "pickup_position", "playing_style")))
    for effect_name in ("space", "modulation", "delay"):
        effect = payload.get(effect_name)
        if isinstance(effect, Mapping) and effect.get("enabled") is True:
            parts.extend((effect_name, str(effect.get("kind", ""))))

    dynamics = payload.get("dynamics", {})
    spectrum = payload.get("spectrum", {})
    if isinstance(dynamics, Mapping) and float(dynamics.get("pick_sensitivity", 0.0)) >= 0.7:
        parts.append("dynamic pick sensitivity")
    if isinstance(spectrum, Mapping):
        if float(spectrum.get("warmth", 0.0)) >= 0.65:
            parts.append("warm")
        if float(spectrum.get("brightness", 0.0)) >= 0.65:
            parts.append("bright")
        if float(spectrum.get("bass_tightness", 0.0)) >= 0.65:
            parts.append("tight bass")
    return " ".join(parts)


def _concepts(query_text: str) -> set[str]:
    normalized = f" {_normalize(query_text)} "
    result = _tokens(query_text)
    for concept, aliases in _CONCEPT_ALIASES.items():
        if any(f" {_normalize(alias)} " in normalized for alias in aliases):
            result.add(concept)
    return result


def _text_fields(value: Mapping[str, Any], fields: Sequence[str]) -> str:
    parts: list[str] = []
    for field in fields:
        item = value.get(field)
        if item is None:
            continue
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            parts.extend(f"{key} {entry}" for key, entry in sorted(item.items(), key=lambda pair: str(pair[0])))
        elif isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            parts.extend(str(entry) for entry in item)
        else:
            parts.append(str(item))
    return " ".join(parts)


def _semantic_score(query: set[str], text: str) -> tuple[float, list[str]]:
    candidate = _tokens(text)
    matched = sorted(query & candidate)
    # Avoid allowing generic format words to dominate specific musical terms.
    ignored = {"amp", "audio", "cab", "guitar", "ir", "model", "mono", "nam", "plugin", "stereo"}
    useful = [word for word in matched if word not in ignored]
    score = min(40.0, len(useful) * 4.0 + (len(matched) - len(useful)) * 0.5)
    reasons = ["mots: " + ", ".join(useful[:8])] if useful else []
    return score, reasons


def _numeric_leaves(value: object, result: dict[str, float] | None = None) -> dict[str, float]:
    result = result if result is not None else {}
    if isinstance(value, Mapping):
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)) and math.isfinite(float(item)):
                result[_normalize(key).replace(" ", "_")] = float(item)
            elif isinstance(item, (Mapping, list, tuple)):
                _numeric_leaves(item, result)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _numeric_leaves(item, result)
    return result


def _fingerprint_score(intent: ToneIntent, asset: Mapping[str, Any]) -> tuple[float, list[str]]:
    metadata = asset.get("metadata") or {}
    profiles = (metadata.get("characterization") or {}).get("profiles", {})
    measured = []
    for profile in profiles.values():
        context = profile.get("context", {})
        if context.get("nam_sha256") != asset.get("sha256"):
            continue
        # Amp-only measurements include a cabinet: score only full-rig profiles
        # until the planner can jointly rank the identical NAM/IR pair.
        if context.get("associated_cab_ir_id"):
            continue
        try:
            from .audio_analysis import score_features
            measured.append(score_features(profile["features"], intent)["total"])
        except (KeyError, TypeError, ValueError):
            continue
    if measured:
        similarity = max(0.0, 1.0 - min(sum(measured) / len(measured), 1.0))
        return 24.0 * similarity, ["DI/rendu appariés: indices contextuels, pas une signature universelle"]
    fingerprint = asset.get("fingerprint") or asset.get("audio_fingerprint")
    if not isinstance(fingerprint, Mapping):
        return 0.0, []
    try:
        # Prefer the shared, versioned distance implementation when a complete
        # AudioFingerprint is attached by the index-enrichment stage.
        from .fingerprints import AudioFingerprint, distance_to_tone_intent

        validated = AudioFingerprint.model_validate(fingerprint)
        distance = distance_to_tone_intent(validated, intent)
        score = 30.0 * max(0.0, 1.0 - min(distance.total, 1.0))
        return score, [f"empreinte: {distance.compared_features} dimension(s)"]
    except (TypeError, ValueError):
        # Partial forward-compatible fingerprints remain useful.  Unknown
        # fields never become executable data; they only influence ranking.
        pass
    payload = _intent_payload(intent)
    # Audio fingerprints describe the source/amp response; time-based effects,
    # confidence and CPU policy must not accidentally influence NAM similarity.
    target = _numeric_leaves(
        {
            key: payload.get(key, {})
            for key in ("gain", "dynamics", "spectrum")
        }
    )
    measured = _numeric_leaves(fingerprint)
    common = sorted(target.keys() & measured.keys())
    if not common:
        return 0.0, []

    similarities: list[float] = []
    for key in common:
        expected, actual = target[key], measured[key]
        if 0.0 <= expected <= 1.0 and 0.0 <= actual <= 1.0:
            similarities.append(1.0 - abs(expected - actual))
        elif -1.0 <= expected <= 1.0 and -1.0 <= actual <= 1.0:
            similarities.append(1.0 - abs(expected - actual) / 2.0)
        else:
            scale = max(abs(expected), abs(actual), 1.0)
            similarities.append(max(0.0, 1.0 - abs(expected - actual) / scale))
    score = 30.0 * sum(similarities) / len(similarities)
    return score, [f"empreinte: {len(common)} dimension(s)"]


def _plugin_text(plugin: Mapping[str, Any]) -> str:
    return _text_fields(plugin, ("name", "uri", "class", "description", "tags", "makes"))


def _plugin_effect_roles(plugin: Mapping[str, Any]) -> set[str]:
    from ..knowledge import plugin_knowledge
    known = plugin_knowledge(plugin)
    if known["role"]:
        return {known["role"]}
    text = _normalize(_plugin_text(plugin))
    roles: set[str] = set()
    resource_roles = set(plugin.get("resource_roles", []))
    if "nam_model" in resource_roles:
        roles.add("amp")
    if "cab_ir" in resource_roles or any(word in text for word in ("cabinet", "cab sim", "ultracab")):
        roles.add("cabinet")
    if "reverb_ir" in resource_roles or "reverb" in text or "freeverb" in text:
        roles.add("reverb")
    checks = {
        "input": ("input stage",),
        "noise_gate": ("noise gate", "expander"),
        "compressor": ("compressor", "sustainer"),
        "pitch": ("pitch", "detune", "octave", " oc 2"),
        "wah": ("wah", "quack"),
        "drive": ("drive", "distortion", "fuzz", "muff", "rat", "screamer", "sd1", "booster"),
        "amp": (" amplifier", " amp ", "preamp", "jcm", "plexi"),
        "eq": (" eq", "equalizer", "tone stack", "tone mono", "tone stereo"),
        "modulation": ("chorus", "flanger", "phaser", "tremolo", "vibrato", "rotary", "detune"),
        "delay": ("delay", "echo"),
        "output": ("volume",),
    }
    padded = f" {text} "
    for role, needles in checks.items():
        if any(needle in padded for needle in needles):
            roles.add(role)
    return roles


def _asset_text(asset: Mapping[str, Any]) -> str:
    text = _text_fields(
        asset,
        ("display_name", "description", "tags", "makes", "gear", "capture_type", "architecture"),
    )
    metadata = asset.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("nam_file"), Mapping):
        text += " " + _text_fields(metadata["nam_file"], ("name", "gear_make", "gear_model", "tone_type", "gear_type"))
    if isinstance(metadata, Mapping) and isinstance(metadata.get("tone3000"), Mapping):
        text += " " + _text_fields(
            metadata["tone3000"],
            ("title", "description", "tags", "makes", "gear", "model_name", "model_size", "architecture"),
        )
    return text


def _live_plugins(capabilities: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = []
    for raw in capabilities.get("plugins", []):
        if not isinstance(raw, Mapping):
            continue
        plugin = dict(raw)
        if plugin.get("allowed_in_live_chain", True) is not True:
            continue
        if plugin.get("uri") in RENDER_ONLY_URIS:
            continue
        if not isinstance(plugin.get("plugin_id"), str):
            continue
        result.append(plugin)
    return result


def _utility_match(plugin: Mapping[str, Any], patterns: Sequence[str]) -> tuple[int, str] | None:
    text = _normalize(_plugin_text(plugin))
    for priority, pattern in enumerate(patterns):
        if _normalize(pattern) in text:
            return priority, pattern
    return None


class CandidateRetriever:
    """Build a small, deterministic and explainable catalog view for Ollama."""

    def __init__(self, max_plugins: int = 24, max_assets_per_role: int = 12):
        if not 8 <= max_plugins <= 64:
            raise ValueError("max_plugins must be between 8 and 64")
        if not 1 <= max_assets_per_role <= 64:
            raise ValueError("max_assets_per_role must be between 1 and 64")
        self.max_plugins = max_plugins
        self.max_assets_per_role = max_assets_per_role

    def retrieve(
        self,
        intent: ToneIntent,
        prompt: str,
        capabilities: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(capabilities.get("catalog"), Mapping):
            raise ValueError("capabilities.catalog is required")

        query_text = _query_text(intent, prompt)
        query = _concepts(query_text)
        intent_payload = _intent_payload(intent)
        constraints = intent_payload.get("chain_constraints", {})
        forbidden_roles = set(constraints.get("forbidden_roles", [])) if isinstance(constraints, Mapping) else set()
        required_roles = set(constraints.get("required_roles", [])) if isinstance(constraints, Mapping) else set()
        allow_nam = constraints.get("allow_nam", True) is True if isinstance(constraints, Mapping) else True
        allow_cab_ir = constraints.get("allow_cab_ir", True) is True if isinstance(constraints, Mapping) else True
        allow_stereo = constraints.get("allow_stereo", True) is True if isinstance(constraints, Mapping) else True
        desired_roles = set(required_roles)
        if allow_nam:
            desired_roles.add("amp")
        if allow_cab_ir:
            desired_roles.add("cabinet")
        gain_payload = intent_payload.get("gain", {})
        from ..knowledge import permits_extra_drive
        if permits_extra_drive(intent, prompt):
            desired_roles.add("drive")
        dynamics_payload = intent_payload.get("dynamics", {})
        if isinstance(dynamics_payload, Mapping) and float(dynamics_payload.get("compression", 0.0)) >= 0.45:
            desired_roles.add("compressor")
        for effect_name, effect_role in (("space", "reverb"), ("modulation", "modulation"), ("delay", "delay")):
            effect_payload = intent_payload.get(effect_name, {})
            if isinstance(effect_payload, Mapping) and effect_payload.get("enabled") is True:
                desired_roles.add(effect_role)
        plugins = []
        for plugin in _live_plugins(capabilities):
            resource_roles = set(plugin.get("resource_roles", []))
            if not allow_nam and "nam_model" in resource_roles:
                continue
            if not allow_cab_ir and "cab_ir" in resource_roles:
                continue
            if not allow_stereo and (
                int(plugin.get("audio_outputs", 0) or 0) > 1 or "stereo" in _normalize(plugin.get("name", ""))
            ):
                continue
            if forbidden_roles & _plugin_effect_roles(plugin):
                continue
            plugins.append(plugin)
        scored: dict[str, tuple[float, list[str], dict[str, Any]]] = {}
        utility_ids: set[str] = set()

        for plugin in plugins:
            score, reasons = _semantic_score(query, _plugin_text(plugin))
            plugin_effect_roles = _plugin_effect_roles(plugin)
            matched_desired = (desired_roles - required_roles) & plugin_effect_roles
            if matched_desired:
                score += 25.0
                reasons.append("rôle musical: " + ", ".join(sorted(matched_desired)))
            matched_required = required_roles & plugin_effect_roles
            if matched_required:
                score += 80.0
                reasons.append("rôle requis: " + ", ".join(sorted(matched_required)))
            roles = [role for role in plugin.get("resource_roles", []) if role in RESOURCE_KIND_BY_ROLE]
            if roles:
                score += 45.0
                reasons.append("hôte de ressource: " + ", ".join(sorted(roles)))
            scored[plugin["plugin_id"]] = (score, reasons, plugin)

        # Guarantee one safe building block per utility function.
        for group_index, (group, patterns) in enumerate(_UTILITY_GROUPS):
            matches = []
            for plugin in plugins:
                match = _utility_match(plugin, patterns)
                if match is not None:
                    matches.append((match[0], _normalize(plugin.get("name", "")), plugin["plugin_id"], plugin))
            if not matches:
                continue
            plugin = min(matches)[3]
            utility_ids.add(plugin["plugin_id"])
            old_score, old_reasons, _ = scored[plugin["plugin_id"]]
            scored[plugin["plugin_id"]] = (
                max(old_score, 100.0 - group_index),
                [f"utilitaire sûr: {group}", *old_reasons],
                plugin,
            )

        all_ordered_plugins = sorted(
            scored.values(),
            key=lambda item: (-item[0], _normalize(item[2].get("name", "")), item[2]["plugin_id"]),
        )
        mandatory_plugins = [item for item in all_ordered_plugins if item[2]["plugin_id"] in utility_ids]
        remaining_plugins = [item for item in all_ordered_plugins if item[2]["plugin_id"] not in utility_ids]
        selected_ids = {
            item[2]["plugin_id"]
            for item in [
                *mandatory_plugins,
                *remaining_plugins[: self.max_plugins - len(mandatory_plugins)],
            ]
        }
        # Rank remains musical-score order while selection itself guarantees the
        # safe utility set even for a deliberately small shortlist.
        ordered_plugins = [
            item for item in all_ordered_plugins if item[2]["plugin_id"] in selected_ids
        ]

        selected_plugins = []
        supported_roles: set[str] = set()
        for rank, (score, reasons, plugin) in enumerate(ordered_plugins, 1):
            supported_roles.update(role for role in plugin.get("resource_roles", []) if role in RESOURCE_KIND_BY_ROLE)
            selected_plugins.append(
                {
                    **copy.deepcopy(plugin),
                    "retrieval_rank": rank,
                    "retrieval_score": round(score, 6),
                    "retrieval_reasons": list(dict.fromkeys(reasons)) or ["complément de chaîne disponible"],
                }
            )

        kind_to_role = {kind: role for role, kind in RESOURCE_KIND_BY_ROLE.items()}
        grouped_assets: dict[str, list[tuple[float, list[str], dict[str, Any]]]] = {
            role: [] for role in sorted(supported_roles, key=lambda role: _ROLE_ORDER.get(role, 99))
        }
        for raw in capabilities.get("assets", []):
            if not isinstance(raw, Mapping) or not isinstance(raw.get("asset_id"), str):
                continue
            role = kind_to_role.get(raw.get("kind"))
            if role not in supported_roles:
                continue
            asset = dict(raw)
            semantic, reasons = _semantic_score(query, _asset_text(asset))
            fingerprint, fingerprint_reasons = _fingerprint_score(intent, asset)
            preference = float(asset.get("user_preference_score", 0))
            score = semantic + fingerprint + max(-8.0, min(8.0, preference))
            if preference:
                reasons.append("préférence enregistrée pour ce profil de guitare et ce fichier")
            grouped_assets[role].append((score, [*reasons, *fingerprint_reasons], asset))

        selected_assets: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for role in sorted(grouped_assets, key=lambda value: _ROLE_ORDER.get(value, 99)):
            candidates = sorted(
                grouped_assets[role],
                key=lambda item: (-item[0], _normalize(item[2].get("display_name", "")), item[2]["asset_id"]),
            )[: self.max_assets_per_role]
            counts[role] = len(candidates)
            for rank, (score, reasons, asset) in enumerate(candidates, 1):
                selected_assets.append(
                    {
                        **copy.deepcopy(asset),
                        "resource_role": role,
                        "retrieval_rank": rank,
                        "retrieval_score": round(score, 6),
                        "retrieval_reasons": list(dict.fromkeys(reasons)) or ["ressource installée compatible"],
                    }
                )

        result: dict[str, Any] = {
            "schema_version": SHORTLIST_SCHEMA_VERSION,
            "catalog": copy.deepcopy(dict(capabilities["catalog"])),
            "plugins": selected_plugins,
            "assets": selected_assets,
            "selection": {
                "max_plugins": self.max_plugins,
                "max_assets_per_role": self.max_assets_per_role,
                "plugin_count": len(selected_plugins),
                "asset_counts_by_role": counts,
            },
        }
        if isinstance(capabilities.get("capability_sha256"), str):
            result["capability_sha256"] = capabilities["capability_sha256"]
        return result


def build_shortlist(
    intent: ToneIntent,
    prompt: str,
    capabilities: Mapping[str, Any],
    *,
    max_plugins: int = 24,
    max_assets_per_role: int = 12,
) -> dict[str, Any]:
    """Functional entry point used by the RTX request pipeline."""

    return CandidateRetriever(max_plugins, max_assets_per_role).retrieve(intent, prompt, capabilities)
