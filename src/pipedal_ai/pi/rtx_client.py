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
                return ProposalSet.model_validate(response.json())
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
