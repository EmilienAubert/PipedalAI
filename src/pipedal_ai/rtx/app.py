from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException

from .. import __version__
from ..config import RTXConfig, load_rtx_config
from ..errors import RemoteServiceError
from ..intent import ToneIntent
from ..models import ProposalSet, RTXProposalRequest, ToneIntentRequest
from ..security import NetworkAndSizeMiddleware, bearer_dependency
from .fingerprints import FingerprintIndex
from .ollama import OllamaClient


def create_app(config: RTXConfig) -> FastAPI:
    app = FastAPI(title="PiPedal AI RTX", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(NetworkAndSizeMiddleware, allowed_cidrs=config.server.allowed_cidrs, max_body_bytes=4 * 1024 * 1024,
                       route_limits={"/api/v1/audio/analyze-pair": 96 * 1024 * 1024})
    authorize = bearer_dependency(config.server.bearer_token)
    fingerprint_index = FingerprintIndex(config.fingerprints.index_path) if config.fingerprints.enabled else None
    ollama = OllamaClient(config.ollama, fingerprint_index=fingerprint_index)
    app.state.ollama = ollama
    from .audio_api import install_audio_routes
    install_audio_routes(app, authorize)

    @app.get("/api/v1/health")
    async def health() -> dict:
        return {"status": "ok", "ollama": await ollama.health(), "version": __version__,
                "text_pipeline": "intent-shortlist-musical-adapters/1.0.0",
                "model": config.ollama.model, "output_format": config.ollama.output_format}

    @app.post("/api/v1/intents/text", response_model=ToneIntent, dependencies=[Depends(authorize)])
    async def intents(request: ToneIntentRequest) -> ToneIntent:
        try:
            return await ollama.extract_intent(request.prompt, request.profile)
        except RemoteServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/v1/proposals/text", response_model=ProposalSet, dependencies=[Depends(authorize)])
    async def proposals(request: RTXProposalRequest) -> ProposalSet:
        try:
            return await ollama.propose(request)
        except RemoteServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("PIPEDAL_AI_RTX_CONFIG", "config/rtx.toml")))
    parser.add_argument("--ssl-certfile", type=Path)
    parser.add_argument("--ssl-keyfile", type=Path)
    parser.add_argument("--ssl-ca-certs", type=Path)
    args = parser.parse_args()
    config = load_rtx_config(args.config)
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
        ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None,
        ssl_ca_certs=str(args.ssl_ca_certs) if args.ssl_ca_certs else None,
        ssl_cert_reqs=2 if args.ssl_ca_certs else 0,
    )


if __name__ == "__main__":
    main()
