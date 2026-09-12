"""Compact LLM decisions -> catalogue-backed, deterministic musical presets."""
from __future__ import annotations

from copy import deepcopy

from ..asset_metadata import capture_info
from ..knowledge import ORDER, KNOWLEDGE_VERSION, adapt_parameters, permits_extra_drive, plugin_knowledge
from ..models import ChainStep, MusicalPlan, PresetSpec, ProposalSet
from .ollama_schema import generation_schema


MUSICAL_SYSTEM = """You are PiPedal AI's musical decision engine. Return only MusicalPlan JSON.
Select a SHORT serial chain from the supplied plugins and assets, respecting their
verified roles. You never write LV2 parameters: deterministic adapters do that.
Copy request_id and catalog. One plugin per role. Effects not requested should be
omitted. Do not add a drive pedal just because an amp should crunch. A fuzz is not
a compressor. Add a cabinet only with a declared amp-only NAM; omit it for full-rig
or unknown captures. nam_candidates are up to three musically suitable alternatives
to the selected NAM, including the selected NAM first. All must be compatible with
the same cabinet policy. Prefer meaningful tone differences within the target.
For an artist name without a song or tonal detail, make a cautious assumption;
never claim an exact reproduction. Keep rationale short and in French.
"""


def compact_shortlist(shortlist, intent, prompt):
    plugins = []
    for plugin in shortlist["plugins"]:
        known = plugin_knowledge(plugin)
        if not known["adapter"]:
            continue
        role = known["role"]
        if role in intent.chain_constraints.forbidden_roles:
            continue
        if role == "drive" and not permits_extra_drive(intent, prompt):
            continue
        if known["adapter"] == "fuzz" and intent.gain.character != "fuzz" and not any(
                word in prompt.casefold() for word in ("fuzz", "axisface", "axis face")):
            continue
        if role == "modulation" and not intent.modulation.enabled and role not in intent.chain_constraints.required_roles:
            continue
        if role == "delay" and not intent.delay.enabled and role not in intent.chain_constraints.required_roles:
            continue
        if role == "reverb" and not intent.space.enabled and role not in intent.chain_constraints.required_roles:
            continue
        if role == "modulation" and known["adapter"] != intent.modulation.kind:
            continue
        if any(not any(a["resource_role"] == r for a in shortlist["assets"]) for r in plugin["resource_roles"]):
            continue
        plugins.append(plugin)
    # One verified host per role is sufficient for this compact decision contract.
    # Keep two EQ choices, without filling context with unrelated alternatives.
    counts = {}
    selected = []
    for plugin in plugins:
        role = plugin_knowledge(plugin)["role"]
        counts[role] = counts.get(role, 0) + 1
        if counts[role] <= (2 if role == "eq" else 1):
            selected.append(plugin)
    assets = []
    asset_counts = {}
    for asset in shortlist["assets"]:
        role = asset["resource_role"]
        asset_counts[role] = asset_counts.get(role, 0) + 1
        if asset_counts[role] <= (6 if role == "nam_model" else 3):
            assets.append(asset)
    return {**shortlist, "plugins": selected, "assets": assets}


def musical_schema(payload):
    schema = generation_schema(MusicalPlan)
    schema["properties"]["request_id"] = {"type": "string", "enum": [payload["request_id"]]}
    for key, value in payload["catalog"].items():
        schema["properties"]["catalog"]["properties"][key] = {"enum": [value]}
    shortlist = payload["candidate_shortlist"]
    branches = []
    for plugin in shortlist["plugins"]:
        props = {"plugin_id": {"type": "string", "enum": [plugin["plugin_id"]]},
                 "role": {"type": "string", "enum": [plugin_knowledge(plugin)["role"]]}}
        roles = plugin["resource_roles"]
        ids = [a["asset_id"] for a in shortlist["assets"] if a["resource_role"] in roles]
        props["asset_id"] = {"type": "string", "enum": ids} if roles else {"type": "null"}
        branches.append({"type": "object", "additionalProperties": False,
                         "properties": props, "required": ["plugin_id", "role", "asset_id"]})
    if not branches:
        raise ValueError("Aucun plugin avec adaptateur musical disponible")
    schema["properties"]["chain"]["items"] = {"anyOf": branches}
    schema["properties"]["chain"]["maxItems"] = min(8, payload["tone_intent"]["chain_constraints"]["max_plugins"])
    nam_ids = [a["asset_id"] for a in shortlist["assets"] if a["resource_role"] == "nam_model"]
    schema["properties"]["nam_candidates"]["items"] = {"type": "string", "enum": nam_ids} if nam_ids else {"type": "string"}
    if not nam_ids:
        schema["properties"]["nam_candidates"]["maxItems"] = 0
    return schema


def musical_input(payload):
    result = deepcopy(payload)
    for plugin in result["candidate_shortlist"]["plugins"]:
        knowledge = plugin_knowledge(plugin)
        keep = {k: plugin[k] for k in ("plugin_id", "name", "resource_roles")}
        plugin.clear()
        plugin.update(keep, role=knowledge["role"], function=knowledge["description"])
    for asset in result["candidate_shortlist"]["assets"]:
        keep = {k: asset[k] for k in ("asset_id", "display_name", "resource_role")}
        keep.update(capture_info(asset))
        keep["reasons"] = asset.get("retrieval_reasons", [])[:3]
        local = (asset.get("metadata") or {}).get("nam_file", {})
        tone = (asset.get("metadata") or {}).get("tone3000", {})
        keep["description"] = str(tone.get("description") or local.get("tone_type") or "")[:180]
        asset.clear()
        asset.update(keep)
    return result


def validate_musical_set(proposal, capabilities, intent, prompt):
    plugins = {p["plugin_id"]: p for p in capabilities["plugins"]}
    assets = {a["asset_id"]: a for a in capabilities["assets"]}
    for spec in proposal.proposals:
        roles = []
        nam = None
        cabinet = False
        for step in spec.chain:
            plugin = plugins[step.plugin_id]
            known = plugin_knowledge(plugin)
            role = known["role"]
            if not known["adapter"]:
                raise ValueError("Plugin sans adaptateur musical vérifié")
            roles.append(role)
            if role == "drive" and not permits_extra_drive(intent, prompt):
                raise ValueError("Pédale de drive non demandée")
            if role in intent.chain_constraints.forbidden_roles:
                raise ValueError("Rôle interdit")
            if role == "amp":
                for resource in step.resources:
                    if resource.role == "nam_model":
                        nam = assets[resource.asset_id]
            cabinet |= role == "cabinet"
        if len(roles) != len(set(roles)):
            raise ValueError("Plusieurs plugins pour un même rôle musical")
        if roles != sorted(roles, key=ORDER.get):
            raise ValueError("Placement musical incohérent")
        if len(roles) > intent.chain_constraints.max_plugins:
            raise ValueError("Chaîne trop longue")
        if set(intent.chain_constraints.required_roles) - set(roles):
            raise ValueError("Effet explicitement requis absent")
        if cabinet and nam and capture_info(nam)["capture_type"] != "amp":
            raise ValueError("Cabinet associé à une capture non déclarée amp-only")
        if nam and capture_info(nam)["capture_type"] == "amp" and not cabinet:
            raise ValueError("Capture amp-only sans cabinet : chaîne incomplète")


def build_musical_set(plan, request, intent, shortlist):
    if plan.request_id != request.request_id or plan.catalog.model_dump(mode="json") != request.capabilities["catalog"]:
        raise ValueError("Corrélation du plan musical modifiée")
    plugins = {p["plugin_id"]: p for p in shortlist["plugins"]}
    assets = {a["asset_id"]: a for a in shortlist["assets"]}
    for choice in plan.chain:
        plugin = plugins.get(choice.plugin_id)
        if plugin is None or plugin_knowledge(plugin)["role"] != choice.role:
            raise ValueError("Rôle/plugin hors liste courte musicale")
        roles = plugin["resource_roles"]
        if roles:
            asset = assets.get(choice.asset_id)
            if len(roles) != 1 or asset is None or asset["resource_role"] != roles[0]:
                raise ValueError("Fichier incompatible avec le plugin sélectionné")
        elif choice.asset_id is not None:
            raise ValueError("Fichier associé à un plugin sans chargeur")
    selected_nam = next((s.asset_id for s in plan.chain if s.role == "amp"), None)
    if plan.nam_candidates and (not selected_nam or plan.nam_candidates[0] != selected_nam):
        raise ValueError("Le premier candidat NAM doit être celui de la chaîne")
    for identifier in plan.nam_candidates:
        if identifier not in assets or assets[identifier]["resource_role"] != "nam_model":
            raise ValueError("Candidat NAM hors liste courte")
    candidates = plan.nam_candidates or ([selected_nam] if selected_nam else [])
    variants = []
    explanations = {"conservative": "Plus ronde, attaque et saturation retenues",
                    "balanced": "Équilibre correspondant à la demande",
                    "bold": "Plus incisive, variation modérée du même objectif"}
    for index, variant in enumerate(explanations):
        chain = []
        for choice in sorted(plan.chain, key=lambda s: ORDER[s.role]):
            plugin = plugins[choice.plugin_id]
            identifier = candidates[min(index, len(candidates) - 1)] if choice.role == "amp" and candidates else choice.asset_id
            resources = [{"role": role, "asset_id": identifier} for role in plugin["resource_roles"]]
            chain.append(ChainStep(instance_id=choice.role, plugin_id=choice.plugin_id,
                                   parameters=adapt_parameters(plugin, intent, variant, request.profile), resources=resources))
        variants.append(PresetSpec(schema_version="pipedal-ai.preset-spec/1.0.0", catalog=plan.catalog,
                                  variant=variant, name=f"AI {variant.title()} - {request.prompt[:40]}",
                                  description=explanations[variant] + ". " + plan.rationale, chain=chain))
    report = {"musical_plan": plan.model_dump(mode="json"), "tone_intent": intent.model_dump(mode="json"),
              "prompt": request.prompt, "knowledge_version": KNOWLEDGE_VERSION,
              "capability_sha256": request.capabilities.get("capability_sha256"),
              "profile_id": (request.profile or {}).get("profile_id"),
              "warnings": [], "choices": [{"role": c.role, "plugin": plugins[c.plugin_id]["name"]} for c in plan.chain]}
    if any(candidates) and any(capture_info(assets[c])["capture_type"] == "unknown" for c in candidates):
        report["warnings"].append("Type de capture NAM inconnu : résultat à écouter avant usage live")
    if candidates and (request.profile or {}).get("nam_input_calibration_dbu") is None:
        report["warnings"].append("Calibration NAM non renseignée : valeur par défaut du plugin, pas une mesure de ton interface")
    if intent.confidence < 0.7:
        report["warnings"].append("Demande ambiguë : préciser le morceau ou clean/crunch/lead améliore le résultat")
    report["variants"] = [{"variant": s.variant, "chain": [
        {"role": step.instance_id, "plugin": plugins[step.plugin_id]["name"],
         "assets": [assets[r.asset_id]["display_name"] for r in step.resources],
         "parameters": step.parameters} for step in s.chain]} for s in variants]
    report["limitations"] = "Choix textuels et métadonnées déclarées; conformité au son non mesurée sans banc DI"
    value = ProposalSet(schema_version="pipedal-ai.proposal-set/1.0.0", request_id=request.request_id,
                        catalog=plan.catalog, proposals=variants, decision_report=report)
    validate_musical_set(value, request.capabilities, intent, request.prompt)
    return value
