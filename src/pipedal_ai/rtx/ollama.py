from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import OllamaConfig
from ..errors import RemoteServiceError
from ..intent import ToneIntent
from ..models import ProposalSet, RTXProposalRequest
from .retrieval import CandidateRetriever, _plugin_effect_roles
from .fingerprints import FingerprintIndex


INTENT_SYSTEM_PROMPT = """You are the musical intent extractor for PiPedal AI.
Convert the user's description and optional guitar profile into exactly one ToneIntent.
Return JSON only, matching the supplied schema. Do not choose plugins, NAM models or IRs.
Interpret subjective words musically, preserve explicit negations, and use conservative
assumptions when information is missing. Values are normalized perceptual targets.
Copy the original prompt exactly. Never emit paths, URLs, commands or extra fields.
"""

PLANNER_SYSTEM_PROMPT = """You are the preset planning engine for PiPedal AI.
Return only one ProposalSet matching the supplied JSON schema. The Raspberry Pi is
the sole authority. Use only plugin_id and asset_id values in the supplied shortlist.
Never emit paths, shell commands, URLs, split routing, render-only plugins or extra
fields. Return exactly three serial chains in this order: conservative, balanced,
bold. Copy request_id and catalog exactly. Use only listed input-control symbols and
respect datatype, range and scale points. Bind every resource role required by a
plugin. Prefer safe gain staging, useful musical differences and short chains.
Add a cabinet IR after a NAM only when its metadata explicitly says it is an
amp-only capture; for unknown or amp-cab captures, do not add another cabinet.
Descriptions must briefly explain the important musical choices in French.
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
        shortlist = self.retriever.retrieve(intent, request.prompt, capabilities)
        payload = {
            "request_id": request.request_id,
            "tone_intent": intent.model_dump(mode="json"),
            "candidate_shortlist": shortlist,
        }

        def validate(value: ProposalSet) -> None:
            self._validate_plan(value, request, intent, shortlist)

        return await self._generate(
            ProposalSet, PLANNER_SYSTEM_PROMPT, payload, validate=validate,
            temperature=self.config.temperature,
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
        for _attempt in range(self.config.max_retries + 1):
            messages = [{"role": "system", "content": system_prompt}]
            if correction:
                messages.append(
                    {
                        "role": "system",
                        "content": "Your previous answer was rejected. Correct it. Reason: " + correction,
                    }
                )
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                }
            )
            body = {
                "model": self.config.model,
                "stream": False,
                "format": model_type.model_json_schema(),
                "messages": messages,
                "options": {"temperature": temperature, "seed": 42, "num_predict": 4096},
            }
            try:
                response = await self._post(f"{self.config.base_url}/api/chat", body)
                response.raise_for_status()
                envelope = response.json()
                content = envelope["message"]["content"]
                value = model_type.model_validate_json(content)
                if validate:
                    validate(value)
                return value
            except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
                last_error = exc
                correction = str(exc).replace("\n", " ")[:600]
        raise RemoteServiceError(f"Réponse Ollama invalide après validation : {last_error}") from last_error

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
                        raise ValueError(f"Paramètre hors liste courte : {step.plugin_id}/{symbol}")
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
