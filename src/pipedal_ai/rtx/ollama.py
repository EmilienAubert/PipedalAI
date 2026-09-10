from __future__ import annotations

import json

import httpx
from pydantic import ValidationError

from ..config import OllamaConfig
from ..errors import RemoteServiceError
from ..models import ProposalSet, RTXProposalRequest


SYSTEM_PROMPT = """You are the proposal engine for PiPedal AI.
Return only a ProposalSet matching the supplied JSON schema.
The Raspberry Pi is the sole authority. Use only plugin_id and asset_id values
present in capabilities. Never emit filesystem paths, shell commands, URLs as
resources, split routing, render-only plugins, or additional fields.
Return exactly three serial-chain proposals in this order: conservative,
balanced, bold. Copy the catalog revision and SHA-256 exactly. Parameters must
use listed input-control symbols and remain within their declared ranges.
Prefer safe output levels and short chains. If the description is ambiguous,
make musically useful conservative assumptions rather than inventing assets.
"""


class OllamaClient:
    def __init__(self, config: OllamaConfig):
        self.config = config

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.config.base_url}/api/tags")
                return response.is_success
        except httpx.HTTPError:
            return False

    async def propose(self, request: RTXProposalRequest) -> ProposalSet:
        user_payload = {
            "request_id": request.request_id,
            "description": request.prompt,
            "guitar_profile": request.profile,
            "catalog": request.capabilities["catalog"],
            "plugins": request.capabilities["plugins"],
            "assets": request.capabilities["assets"],
        }
        body = {
            "model": self.config.model,
            "stream": False,
            "format": ProposalSet.model_json_schema(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))},
            ],
            "options": {"temperature": self.config.temperature},
        }
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(f"{self.config.base_url}/api/chat", json=body)
                response.raise_for_status()
                envelope = response.json()
            content = envelope["message"]["content"]
            proposal = ProposalSet.model_validate_json(content)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteServiceError(f"Réponse Ollama invalide : {exc}") from exc
        if proposal.request_id != request.request_id:
            raise RemoteServiceError("Ollama a modifié request_id.")
        if proposal.catalog.model_dump() != request.capabilities["catalog"]:
            raise RemoteServiceError("Ollama a modifié la référence de catalogue.")
        return proposal

