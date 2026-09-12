from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import connect

from ..config import PiPedalConfig
from ..errors import RemoteServiceError


class PiPedalClient:
    def __init__(self, config: PiPedalConfig):
        self.config = config

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(self.config.http_base_url)
                return response.status_code < 500
        except httpx.HTTPError:
            return False

    async def import_preset(self, path: Path) -> int:
        if not self.config.allow_import:
            raise RemoteServiceError("Import PiPedal désactivé par configuration.")
        size = path.stat().st_size
        if size > self.config.max_upload_bytes:
            raise RemoteServiceError(f"Preset trop grand pour PiPedal ({size} octets).")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.config.http_base_url}{self.config.upload_path}",
                    content=path.read_bytes(),
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                instance_id = int(response.text.strip())
                if instance_id < 0:
                    raise ValueError("negative instance id")
                return instance_id
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise RemoteServiceError(f"Échec d'import PiPedal : {exc}") from exc

    async def _request(self, websocket: Any, message: str, body: Any = None, sequence: int = 1) -> Any:
        envelope = {"message": message, "replyTo": sequence}
        payload = [envelope] if body is None else [envelope, body]
        await websocket.send(json.dumps(payload, separators=(",", ":")))
        while True:
            incoming = json.loads(await asyncio.wait_for(websocket.recv(), timeout=10))
            if isinstance(incoming, list) and incoming and incoming[0].get("reply") == sequence:
                if incoming[0].get("message") == "error":
                    raise RemoteServiceError(str(incoming[1] if len(incoming) > 1 else "PiPedal error"))
                if len(incoming) > 1:
                    return incoming[1]
                return None

    async def activate(self, instance_id: int) -> dict[str, int]:
        if not self.config.allow_activation:
            raise RemoteServiceError("Activation PiPedal désactivée par configuration.")
        previous: int | None = None
        try:
            async with connect(self.config.websocket_url, open_timeout=5, close_timeout=2) as websocket:
                presets = await self._request(websocket, "getPresets", sequence=1)
                previous = int(presets["selectedInstanceId"])
                await websocket.send(json.dumps([{"message": "loadPreset"}, instance_id]))
                verified = await self._request(websocket, "getPresets", sequence=2)
                selected = int(verified["selectedInstanceId"])
                if selected != instance_id:
                    raise RemoteServiceError(f"PiPedal a sélectionné {selected}, pas {instance_id}.")
                return {"previous_instance_id": previous, "selected_instance_id": selected}
        except Exception as exc:
            if previous is not None:
                try:
                    async with connect(self.config.websocket_url, open_timeout=3) as rollback:
                        await rollback.send(json.dumps([{"message": "loadPreset"}, previous]))
                except Exception:
                    pass
            if isinstance(exc, RemoteServiceError):
                raise
            raise RemoteServiceError(f"Échec d'activation PiPedal : {exc}") from exc
