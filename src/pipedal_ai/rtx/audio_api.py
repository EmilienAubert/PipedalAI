from __future__ import annotations

import asyncio
import base64
import hashlib
import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import Field

from ..models import CatalogRef, StrictModel
from .audio_analysis import analyze_pair


class AudioPairRequest(StrictModel):
    schema_version: str = Field(pattern=r"^pipedal-ai.audio-pair/1\.0\.0$")
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    catalog: CatalogRef
    di_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    render_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    di_wav: str = Field(max_length=48000000)
    render_wav: str = Field(max_length=48000000)


def decode_audio_pair(request):
    with tempfile.TemporaryDirectory(prefix="pipedal-ai-audio-") as directory:
        paths = []
        for name, encoded, digest in (("di", request.di_wav, request.di_sha256),
                                      ("render", request.render_wav, request.render_sha256)):
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("Audio trop grand ou SHA-256 incorrect")
            path = Path(directory) / (name + ".wav")
            path.write_bytes(raw)
            paths.append(path)
        return {"request_id": request.request_id, "catalog": request.catalog.model_dump(mode="json"),
                "analysis": analyze_pair(*paths)}


def install_audio_routes(app, authorize):
    semaphore = asyncio.Semaphore(1)
    @app.post("/api/v1/audio/analyze-pair", dependencies=[Depends(authorize)])
    async def analyze(request: AudioPairRequest):
        async with semaphore:
            try:
                return await asyncio.to_thread(decode_audio_pair, request)
            except (ValueError, RuntimeError, OSError) as exc:
                raise HTTPException(422, str(exc)) from exc
