from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Mapping
from typing import TypeVar

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ValidationError

from ..config import OllamaConfig
from ..errors import RemoteServiceError
from ..intent import ToneIntent
from ..models import MusicalPlan, PlanDraft, PresetSpec, ProposalSet, RTXProposalRequest
from .retrieval import CandidateRetriever, _plugin_effect_roles
from .fingerprints import FingerprintIndex
from .diagnostics import save_exchange
from .ollama_schema import generation_schema, planning_schema


logger = logging.getLogger(__name__)


INTENT_SYSTEM_PROMPT = """You are the musical intent extractor for PiPedal AI.
Convert the user's description and optional guitar profile into exactly one ToneIntent.
Return JSON only, matching the supplied schema. Do not choose plugins, NAM models or IRs.
Interpret subjective words musically, preserve explicit negations, and use conservative
assumptions when information is missing. Values are normalized perceptual targets.
Copy the original prompt exactly. Never emit paths, URLs, commands or extra fields.
Do not invent mandatory chain roles: required_roles lists only explicitly requested
effects, not a default amp/cab/input/output recipe. Unknown NAM capture types may
not support adding a cabinet. A preference is not a hard requirement.
"""

PLANNER_SYSTEM_PROMPT = """You are the preset planning engine for PiPedal AI.
Return only one PlanDraft matching the supplied JSON schema. The Raspberry Pi is
the sole authority. Use only plugin_id and asset_id values in the supplied shortlist.
Never emit paths, shell commands, URLs, split routing, render-only plugins or extra
fields. Return exactly three serial chains in this order: conservative, balanced,
bold in the variants array. Copy request_id and catalog exactly. Do not return a
catalog summary, a tool call/result, selected_assets, items, or an outer wrapper.
Use only listed input-control symbols and
respect datatype, range and scale points. Bind every resource role required by a
plugin. Prefer safe gain staging, useful musical differences and short chains.
Add a cabinet IR after a NAM only when its metadata explicitly says it is an
amp-only capture; for unknown or amp-cab captures, do not add another cabinet.
Descriptions must briefly explain the important musical choices in French.
All three variants must stay close to the requested genre, gain and dynamics.
Bold means a modest variation of that same target, NOT a switch to metal/high gain.
Do not invent a compressor or use an amplifier as a file loader. The plugin names,
exact parameter symbols, and resource_roles identify their actual functions.
Use at most one NAM and one cabinet IR in this serial MVP; never stack cabinets.
"""

ModelT = TypeVar("ModelT", bound=BaseModel)


class OllamaClient:
    """Two-stage text engine: semantic intent, then catalog-constrained planning."""

    def __init__(
        self,
        config: OllamaConfig,
        http_client: httpx.AsyncClient | None = None,
        fingerprint_index: FingerprintIndex | None = None,
    ):
        self.config = config
        self._http_client = http_client
        self._fingerprint_index = fingerprint_index
        self.retriever = CandidateRetriever(
            max_plugins=config.max_plugin_candidates,
            max_assets_per_role=config.max_assets_per_role,
        )

    async def health(self) -> bool:
        try:
            response = await self._get(f"{self.config.base_url}/api/tags", timeout=3)
            return response.is_success
        except httpx.HTTPError:
            return False

    async def extract_intent(self, prompt: str, profile: Mapping | None = None) -> ToneIntent:
        payload = {"prompt": prompt, "guitar_profile": dict(profile) if profile else None}

        def validate(value: ToneIntent) -> None:
            if value.prompt != prompt:
                raise ValueError("Le modèle a modifié le prompt original.")

        return await self._generate(
            ToneIntent, INTENT_SYSTEM_PROMPT, payload, validate=validate, temperature=0.0
        )

    async def propose(self, request: RTXProposalRequest) -> ProposalSet:
        intent = await self.extract_intent(request.prompt, request.profile)
        capabilities = self._with_fingerprints(request.capabilities)
        nam_host = next((p for p in capabilities.get("plugins", []) if p["uri"] == "http://two-play.com/plugins/toob-nam"), None)
        if nam_host:
            expected = {c["symbol"]: c["default"] for c in nam_host["controls"] if "Calibration" in c["symbol"] or c["symbol"] == "calibration"}
            calibration = (request.profile or {}).get("nam_input_calibration_dbu")
            if calibration is not None:
                expected.update(inputCalibrationMode=1, calibration=calibration)
            for asset in capabilities.get("assets", []):
                meta = (asset.get("metadata") or {}).get("characterization")
                if meta:
                    meta["profiles"] = {k: p for k, p in meta.get("profiles", {}).items()
                        if all(p.get("context", {}).get("parameters", {}).get(symbol) == value for symbol, value in expected.items())}
        for asset in capabilities.get("assets", []):
            asset["user_preference_score"] = sum(float(p.get("score", 0)) for p in request.preferences
                if p.get("asset_sha256") == asset.get("sha256"))
        shortlist = self.retriever.retrieve(intent, request.prompt, capabilities)
        if self.config.planning_mode == "musical":
            from .musical import MUSICAL_SYSTEM, build_musical_set, compact_shortlist
            shortlist = compact_shortlist(shortlist, intent, request.prompt)
            payload = {"request_id": request.request_id, "catalog": request.capabilities["catalog"],
                       "tone_intent": intent.model_dump(mode="json"), "candidate_shortlist": shortlist}
            def validate_musical(value):
                proposal = build_musical_set(value, request, intent, shortlist)
                self._validate_plan(proposal, request, intent, shortlist)
            plan = await self._generate(MusicalPlan, MUSICAL_SYSTEM, payload,
                                        validate=validate_musical, temperature=self.config.temperature)
            return build_musical_set(plan, request, intent, shortlist)
        if not shortlist.get("plugins"):
            raise RemoteServiceError("Aucun plugin disponible pour cette intention.")
        payload = {
            "request_id": request.request_id,
            "catalog": request.capabilities["catalog"],
            "tone_intent": intent.model_dump(mode="json"),
            "candidate_shortlist": _planning_context(shortlist),
        }

        def validate(value: PlanDraft) -> None:
            proposal = self._proposal_from_draft(value, request)
            self._validate_plan(proposal, request, intent, shortlist)

        draft = await self._generate(
            PlanDraft, PLANNER_SYSTEM_PROMPT, payload, validate=validate,
            temperature=self.config.temperature,
        )
        return self._proposal_from_draft(draft, request)

    @staticmethod
    def _proposal_from_draft(draft: PlanDraft, request: RTXProposalRequest) -> ProposalSet:
        if draft.request_id != request.request_id:
            raise ValueError("Ollama a modifié request_id.")
        if draft.catalog.model_dump(mode="json") != request.capabilities["catalog"]:
            raise ValueError("Ollama a modifié la référence de catalogue.")
        prefixes = {"conservative": "AI Conservative", "balanced": "AI Balanced", "bold": "AI Bold"}
        return ProposalSet(
            schema_version="pipedal-ai.proposal-set/1.0.0",
            request_id=draft.request_id, catalog=draft.catalog,
            proposals=[PresetSpec(
                schema_version="pipedal-ai.preset-spec/1.0.0", catalog=draft.catalog,
                variant=variant.variant,
                name=f"{prefixes[variant.variant]} - {request.prompt[:40]}",
                description=variant.description, chain=variant.chain,
            ) for variant in draft.variants],
        )

    def _with_fingerprints(self, capabilities: Mapping) -> dict:
        result = copy.deepcopy(dict(capabilities))
        if self._fingerprint_index is None:
            return result
        for asset in result.get("assets", []):
            if not isinstance(asset, dict) or not isinstance(asset.get("sha256"), str):
                continue
            fingerprint = self._fingerprint_index.get(asset["sha256"])
            if fingerprint is not None:
                asset["audio_fingerprint"] = fingerprint.model_dump(mode="json")
        return result

    async def _generate(
        self,
        model_type: type[ModelT],
        system_prompt: str,
        payload: Mapping,
        *,
        validate: Callable[[ModelT], None] | None = None,
        temperature: float,
    ) -> ModelT:
        last_error: Exception | None = None
        correction = ""
        previous_content = ""
        schema = generation_schema(model_type)
        if model_type is PlanDraft:
            try:
                schema = planning_schema(payload)
            except ValueError as exc:
                raise RemoteServiceError(f"Schéma de planification impossible : {exc}") from exc
        elif model_type is MusicalPlan:
            from .musical import musical_schema
            try:
                schema = musical_schema(payload)
            except ValueError as exc:
                raise RemoteServiceError(f"Schéma musical impossible : {exc}") from exc
        elif model_type is ToneIntent:
            schema["properties"]["prompt"] = {"type": "string", "enum": [payload["prompt"]]}
        stage = "plan" if model_type in (PlanDraft, MusicalPlan) else "intent"
        for attempt in range(1, self.config.max_retries + 2):
            messages = [{"role": "system", "content": system_prompt +
                         "\nOutput JSON schema:\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))}]
            from .musical import musical_input
            prompt_payload = (musical_input(payload) if model_type is MusicalPlan else
                              _prompt_payload(payload) if model_type is PlanDraft else payload)
            user_content = ("Generate the requested " + model_type.__name__ +
                            " now, using the following input data. Do not summarize this data.\n" +
                            json.dumps(prompt_payload,
                                       ensure_ascii=False, separators=(",", ":")))
            messages.append({"role": "user", "content": user_content})
            if correction:
                if previous_content and model_type not in (PlanDraft, MusicalPlan):
                    messages.append({"role": "assistant", "content": previous_content[:16000]})
                messages.append({"role": "user", "content":
                                 "Your answer was rejected. Return a complete corrected " +
                                 model_type.__name__ + " JSON object, no wrapper. Reason: " + correction})
            # Conservative character-based estimate, not a model-specific tokenizer.
            # Never send a visibly overfull prompt. Musical decisions need much less
            # room than raw LV2 plans; cap output independently of a user's large setting.
            estimate = sum(len(m["content"]) for m in messages) // 2 + 128
            reserve = 1800 if model_type is MusicalPlan else 2048 if model_type is ToneIntent else 4096
            if estimate + reserve > self.config.num_ctx:
                raise RemoteServiceError(
                    f"Contexte estimé trop chargé étape={stage}: estimation={estimate}, "
                    f"réserve={reserve}, num_ctx={self.config.num_ctx}. "
                    "Réduire la liste courte ou augmenter num_ctx; ne pas augmenter num_predict.")
            body = {
                "model": self.config.model,
                "stream": False,
                "format": schema if self.config.output_format == "schema" else "json",
                "messages": messages,
                "options": {"temperature": temperature, "seed": 42,
                            "num_predict": min(self.config.num_predict, self.config.num_ctx - estimate),
                            "num_ctx": self.config.num_ctx},
            }
            if self.config.think is not None:
                body["think"] = self.config.think
            response = None
            content = ""
            try:
                response = await self._post(f"{self.config.base_url}/api/chat", body)
                response.raise_for_status()
                envelope = response.json()
                if "error" in envelope:
                    raise ValueError(f"Ollama error: {envelope['error']}")
                if envelope.get("done_reason") == "length":
                    used_in = envelope.get("prompt_eval_count")
                    used_out = envelope.get("eval_count")
                    if isinstance(used_in, int) and isinstance(used_out, int) and used_in + used_out >= self.config.num_ctx:
                        raise ValueError(f"Réponse tronquée : contexte saturé num_ctx={self.config.num_ctx}, "
                                         f"entrée={used_in}, sortie={used_out}. Réduire le contexte.")
                    if isinstance(used_out, int) and used_out >= body["options"]["num_predict"]:
                        raise ValueError(f"Réponse tronquée : limite num_predict={body['options']['num_predict']} atteinte.")
                    raise ValueError(f"Réponse tronquée signalée par Ollama; cause non déterminée "
                                     f"(entrée={used_in}, sortie={used_out}, num_ctx={self.config.num_ctx}, "
                                     f"num_predict={body['options']['num_predict']}).")
                if envelope.get("done") is False:
                    raise ValueError("Ollama a renvoyé une réponse incomplète.")
                content = envelope["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Réponse finale vide (message.content); le champ thinking n'est pas une réponse JSON.")
                value = model_type.model_validate_json(content)
                if validate:
                    validate(value)
                if model_type in (PlanDraft, MusicalPlan):
                    Draft202012Validator(schema).validate(json.loads(content))
                save_exchange(self.config.diagnostics_directory, stage, attempt, body,
                              response.text, response.status_code, None)
                return value
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:2000]
                trace = save_exchange(self.config.diagnostics_directory, stage, attempt, body,
                                      exc.response.text, exc.response.status_code, detail)
                # A 400 may indicate a missing model or invalid option, not an
                # unsupported schema. Never silently remove grammar constraints.
                logger.warning("Ollama stage=%s HTTP=%s trace=%s", stage, exc.response.status_code, trace)
                raise RemoteServiceError(
                    f"Ollama étape={stage} HTTP {exc.response.status_code} : {detail}"
                ) from exc
            except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError, SchemaValidationError) as exc:
                last_error = exc
                # Pydantic input values may contain private catalogue/prompt data.
                if isinstance(exc, SchemaValidationError):
                    correction = (f"Contrat catalogue : chemin={list(exc.absolute_path)}, "
                                  f"contrainte={exc.validator}. Respecter exactement le schéma fourni.")[:1800]
                elif isinstance(exc, ValidationError):
                    errors = exc.errors(include_input=False, include_url=False, include_context=False)
                    correction = json.dumps(errors, ensure_ascii=False)[:1800]
                else:
                    correction = str(exc).replace("\n", " ")[:1800] or type(exc).__name__
                previous_content = content if isinstance(content, str) else ""
                trace = save_exchange(self.config.diagnostics_directory, stage, attempt, body,
                                      response.text if response is not None else None,
                                      response.status_code if response is not None else None, correction)
                logger.warning("Ollama stage=%s attempt=%s/%s trace=%s rejected: %s",
                               stage, attempt, self.config.max_retries + 1, trace, correction)
        raise RemoteServiceError(
            f"Réponse Ollama invalide étape={stage} après {self.config.max_retries + 1} tentative(s) : {correction}"
        ) from last_error

    async def _get(self, url: str, *, timeout: float) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.get(url, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(url)

    async def _post(self, url: str, body: Mapping) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(url, json=body, timeout=self.config.timeout_seconds)
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            return await client.post(url, json=body)

    @staticmethod
    def _validate_plan(
        proposal: ProposalSet,
        request: RTXProposalRequest,
        intent: ToneIntent,
        shortlist: Mapping,
    ) -> None:
        if proposal.request_id != request.request_id:
            raise ValueError("Ollama a modifié request_id.")
        if proposal.catalog.model_dump(mode="json") != request.capabilities["catalog"]:
            raise ValueError("Ollama a modifié la référence de catalogue.")

        plugins = {
            item["plugin_id"]: item
            for item in shortlist.get("plugins", [])
            if isinstance(item, Mapping) and isinstance(item.get("plugin_id"), str)
        }
        assets = {
            item["asset_id"]: item
            for item in shortlist.get("assets", [])
            if isinstance(item, Mapping) and isinstance(item.get("asset_id"), str)
        }
        for preset in proposal.proposals:
            if len(preset.chain) > intent.chain_constraints.max_plugins:
                raise ValueError(f"La chaîne {preset.variant} dépasse ToneIntent.max_plugins.")
            for step in preset.chain:
                plugin = plugins.get(step.plugin_id)
                if plugin is None:
                    raise ValueError(f"Plugin hors liste courte : {step.plugin_id}")
                controls = {
                    control.get("symbol"): control
                    for control in plugin.get("controls", [])
                    if isinstance(control, Mapping) and isinstance(control.get("symbol"), str)
                }
                for symbol, value in step.parameters.items():
                    control = controls.get(symbol)
                    if control is None:
                        raise ValueError(
                            f"Paramètre hors liste courte : {step.plugin_id}/{symbol}. "
                            f"Plugin={plugin.get('name')}; symboles autorisés={sorted(controls)}"
                        )
                    _validate_control_value(step.plugin_id, control, value)

                required_roles = set(plugin.get("resource_roles", []))
                supplied_roles = {resource.role for resource in step.resources}
                if supplied_roles != required_roles:
                    raise ValueError(
                        f"Ressources incohérentes pour {step.plugin_id}: "
                        f"attendu={sorted(required_roles)}, reçu={sorted(supplied_roles)}"
                    )
                for resource in step.resources:
                    asset = assets.get(resource.asset_id)
                    if asset is None:
                        raise ValueError(f"Ressource hors liste courte : {resource.asset_id}")
                    if asset.get("resource_role") != resource.role:
                        raise ValueError(f"Rôle de ressource incohérent : {resource.asset_id}")

            present_roles = {
                role
                for step in preset.chain
                for role in _plugin_effect_roles(plugins[step.plugin_id])
            }
            if len(nam_steps := [step for step in preset.chain if any(r.role == "nam_model" for r in step.resources)]) > 1:
                raise ValueError("Un seul NAM autorisé par chaîne série dans ce MVP.")
            cabinets = [step for step in preset.chain if "cabinet" in _plugin_effect_roles(plugins[step.plugin_id])]
            if len(cabinets) > 1:
                raise ValueError("Un seul cabinet autorisé par chaîne série dans ce MVP.")
            missing_roles = set(intent.chain_constraints.required_roles) - present_roles
            if missing_roles:
                raise ValueError(f"Rôles obligatoires absents : {sorted(missing_roles)}")

            nam_assets = [
                assets[resource.asset_id]
                for step in preset.chain
                for resource in step.resources
                if resource.role == "nam_model"
            ]
            has_cabinet_ir = any(
                resource.role == "cab_ir" for step in preset.chain for resource in step.resources
            )
            if has_cabinet_ir and any(_capture_type(asset) != "amp" for asset in nam_assets):
                raise ValueError("Une IR de cabinet exige un NAM explicitement marqué amp-only.")
            if cabinets and nam_assets and any(_capture_type(asset) != "amp" for asset in nam_assets):
                raise ValueError("Un cabinet après un NAM exige une capture explicitement amp-only.")
            if cabinets and nam_steps and preset.chain.index(cabinets[0]) < preset.chain.index(nam_steps[0]):
                raise ValueError("Le cabinet doit être placé après le NAM.")


def _planning_context(shortlist: Mapping) -> dict:
    """Send only decision-relevant data, not a retrieval report to be echoed."""
    plugin_keys = {"plugin_id", "name", "class", "uri", "audio_inputs", "audio_outputs", "controls", "resource_roles"}
    asset_keys = {"asset_id", "display_name", "kind", "resource_role", "capture_type", "metadata",
                  "audio_fingerprint", "retrieval_reasons"}
    return {
        "plugins": [{key: value for key, value in item.items() if key in plugin_keys}
                    for item in shortlist.get("plugins", [])],
        "assets": [{key: value for key, value in item.items() if key in asset_keys}
                   for item in shortlist.get("assets", [])],
    }


def _prompt_payload(payload: Mapping) -> dict:
    """Constraints are already in the schema; avoid repeating full LV2 descriptors."""
    result = copy.deepcopy(dict(payload))
    plugins = result["candidate_shortlist"]["plugins"]
    for plugin in plugins:
        plugin["effect_roles"] = sorted(_plugin_effect_roles(plugin))
        plugin.pop("controls", None)
        plugin.pop("uri", None)
    for asset in result["candidate_shortlist"]["assets"]:
        # Audio features have already contributed to retrieval; keep concise reasons.
        asset.pop("audio_fingerprint", None)
        metadata = asset.pop("metadata", None)
        asset["capture_type"] = _capture_type({**asset, "metadata": metadata})
        if isinstance(metadata, Mapping) and isinstance(metadata.get("tone3000"), Mapping):
            info = metadata["tone3000"]
            asset["tone3000"] = {key: str(info[key])[:240] for key in ("title", "gear", "description") if info.get(key)}
    return result


def _validate_control_value(plugin_id: str, control: Mapping, value: object) -> None:
    datatype = control.get("datatype", "number")
    label = f"{plugin_id}/{control.get('symbol', '?')}"
    if datatype == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{label} doit être booléen.")
        numeric: int | float = int(value)
    elif datatype in {"integer", "enumeration"}:
        if type(value) is not int:
            raise ValueError(f"{label} doit être entier.")
        numeric = value
        points = control.get("scale_points") or []
        if datatype == "enumeration" and points:
            allowed = {point.get("value") for point in points if isinstance(point, Mapping)}
            if value not in allowed:
                raise ValueError(f"Énumération refusée : {label}={value}")
    else:
        if type(value) not in {int, float}:
            raise ValueError(f"{label} doit être numérique.")
        numeric = value
    minimum = control.get("minimum")
    maximum = control.get("maximum")
    default = control.get("default")
    if minimum is not None and maximum is not None:
        if not (minimum <= numeric <= maximum or numeric == default):
            raise ValueError(f"Valeur hors plage : {label}={numeric}")


def _capture_type(asset: Mapping) -> str:
    from ..asset_metadata import capture_info
    return capture_info(dict(asset))["capture_type"]


def _legacy_capture_type(asset: Mapping) -> str:
    direct = asset.get("capture_type")
    if isinstance(direct, str):
        return direct.casefold().replace("_", "-")
    metadata = asset.get("metadata")
    if isinstance(metadata, Mapping):
        tone3000 = metadata.get("tone3000")
        if isinstance(tone3000, Mapping) and isinstance(tone3000.get("gear"), str):
            gear = tone3000["gear"].casefold()
            if gear in {"amp", "amp-cab"}:
                return gear
    return "unknown"
