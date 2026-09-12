from __future__ import annotations

import ssl

import httpx

from ..config import RTXClientConfig
from ..errors import RemoteServiceError
from ..models import ProposalSet, RTXProposalRequest


class RTXClient:
    def __init__(self, config: RTXClientConfig):
        self.config = config

    def _ssl_context(self) -> ssl.SSLContext | bool:
        if not self.config.base_url.startswith("https://"):
            return True
        context = ssl.create_default_context(cafile=str(self.config.ca_file) if self.config.ca_file else None)
        if self.config.client_cert_file and self.config.client_key_file:
            context.load_cert_chain(str(self.config.client_cert_file), str(self.config.client_key_file))
        return context

    async def health(self) -> bool:
        if not self.config.enabled:
            return False
        try:
            async with httpx.AsyncClient(verify=self._ssl_context(), timeout=3.0) as client:
                response = await client.get(f"{self.config.base_url}/api/v1/health")
                return response.is_success
        except (httpx.HTTPError, OSError, ssl.SSLError):
            return False

    async def propose(self, request: RTXProposalRequest) -> ProposalSet:
        if not self.config.enabled:
            raise RemoteServiceError("Service RTX désactivé.")
        headers = {"Authorization": f"Bearer {self.config.bearer_token}"}
        try:
            async with httpx.AsyncClient(
                verify=self._ssl_context(), timeout=self.config.timeout_seconds
            ) as client:
                response = await client.post(
                    f"{self.config.base_url}/api/v1/proposals/text",
                    headers=headers,
                    json=request.model_dump(mode="json"),
                )
                response.raise_for_status()
                proposal = ProposalSet.model_validate(response.json())
                if proposal.decision_report.get("musical_plan"):
                    report = proposal.decision_report
                    if report.get("profile_id") != (request.profile or {}).get("profile_id") or report.get("prompt") != request.prompt:
                        raise ValueError("Profil/prompt du rapport musical différent du travail")
                    if report.get("capability_sha256") != request.capabilities.get("capability_sha256"):
                        raise ValueError("Hash des connaissances différent du travail")
                    if report.get("tone_intent", {}).get("prompt") != request.prompt:
                        raise ValueError("Prompt de l'intention modifié")
                return proposal
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("detail", exc.response.text)
            except (ValueError, AttributeError):
                detail = exc.response.text
            # Do not hide the upstream contract failure behind a generic HTTP 502.
            raise RemoteServiceError(
                f"RTX HTTP {exc.response.status_code} : {str(detail)[:2000]}"
            ) from exc
        except (httpx.HTTPError, ValueError, OSError, ssl.SSLError) as exc:
            raise RemoteServiceError(f"RTX indisponible ou réponse invalide : {exc}") from exc

    async def analyze_pair(self, di_path, render_path, catalog, request_id):
        import base64
        from ..audio_io import digest_file
        if not self.config.enabled:
            raise RemoteServiceError("Analyse RTX désactivée")
        if max(di_path.stat().st_size, render_path.stat().st_size) > 32 * 1024 * 1024:
            raise RemoteServiceError("Paire audio trop grande : réduire la DI (32 Mio par WAV maximum)")
        body = {"schema_version": "pipedal-ai.audio-pair/1.0.0", "request_id": request_id,
                "catalog": catalog.model_dump(mode="json"), "di_sha256": digest_file(di_path),
                "render_sha256": digest_file(render_path),
                "di_wav": base64.b64encode(di_path.read_bytes()).decode("ascii"),
                "render_wav": base64.b64encode(render_path.read_bytes()).decode("ascii")}
        try:
            async with httpx.AsyncClient(verify=self._ssl_context(), timeout=self.config.timeout_seconds) as client:
                response = await client.post(self.config.base_url + "/api/v1/audio/analyze-pair",
                    headers={"Authorization": f"Bearer {self.config.bearer_token}"}, json=body)
                response.raise_for_status()
                value = response.json()
                if value.get("request_id") != request_id or value.get("catalog") != body["catalog"]:
                    raise ValueError("Corrélation de l'analyse incorrecte")
                if value["analysis"]["di_sha256"] != body["di_sha256"] or value["analysis"]["render_sha256"] != body["render_sha256"]:
                    raise ValueError("Hash de l'analyse incorrect")
                return value["analysis"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise RemoteServiceError(f"Analyse RTX échouée : {exc}") from exc
