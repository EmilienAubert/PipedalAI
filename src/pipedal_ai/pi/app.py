from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..catalog import CatalogService
from ..compiler import PresetCompiler
from ..config import PiConfig, load_pi_config
from ..db import Database
from ..models import GuitarProfile, GuitarProfileCreate, TextPresetJobRequest
from ..errors import PiPedalAIError
from ..security import NetworkAndSizeMiddleware, api_key_dependency
from .jobs import JobManager, now
from .pipedal_client import PiPedalClient
from .rtx_client import RTXClient
from .system_guard import SystemGuard


def create_app(config: PiConfig) -> FastAPI:
    database = Database(config.storage.database)
    database.initialize()
    if config.storage.artifact_root.is_symlink():
        raise RuntimeError("artifact_root ne doit pas être un lien symbolique")
    config.storage.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    catalog = CatalogService(database, config.policy.max_chain_length)
    compiler = PresetCompiler(
        catalog, config.storage.upload_root, config.policy.max_artifact_uncompressed_bytes,
        config.policy.default_input_volume_db, config.policy.default_output_volume_db,
    )
    rtx = RTXClient(config.rtx)
    pipedal = PiPedalClient(config.pipedal)
    guard = SystemGuard(config.storage.artifact_root, config.policy.max_load_per_cpu, config.policy.min_free_mb)
    manager = JobManager(database, catalog, compiler, rtx, pipedal, guard, config.storage.artifact_root)
    require_key = api_key_dependency(config.server.api_key)

    app = FastAPI(title="PiPedal AI — Pi Authority", version="1.0.0")
    app.add_middleware(NetworkAndSizeMiddleware, allowed_cidrs=config.server.allowed_cidrs, max_body_bytes=2_000_000)
    app.state.database = database
    app.state.catalog = catalog
    app.state.jobs = manager

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "role": "pi-authority", "system": guard.snapshot(),
                "rtx_available": await rtx.health(), "pipedal_available": await pipedal.health()}

    @app.get("/api/v1/status", dependencies=[Depends(require_key)])
    async def status() -> dict:
        active = catalog.active_ref()
        return {"catalog": active.model_dump(), "system": guard.snapshot(),
                "rtx_available": await rtx.health(), "pipedal_available": await pipedal.health(),
                "import_enabled": config.pipedal.allow_import,
                "activation_enabled": config.pipedal.allow_activation}

    @app.get("/api/v1/catalog/capabilities", dependencies=[Depends(require_key)])
    def capabilities() -> dict:
        return catalog.capabilities()

    @app.get("/api/v1/profiles", dependencies=[Depends(require_key)])
    def profiles() -> list[dict]:
        with database.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM guitar_profiles ORDER BY name")]

    @app.post("/api/v1/profiles", dependencies=[Depends(require_key)], status_code=201)
    def create_profile(value: GuitarProfileCreate) -> GuitarProfile:
        profile = GuitarProfile(profile_id=database.new_id("gtr"), created_at=now(), **value.model_dump())
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO guitar_profiles VALUES(?,?,?,?,?,?,?)",
                (profile.profile_id, profile.name, profile.guitar, profile.pickup,
                 profile.input_trim_db, profile.notes, profile.created_at),
            )
        return profile

    @app.delete("/api/v1/profiles/{profile_id}", dependencies=[Depends(require_key)], status_code=204)
    def delete_profile(profile_id: str) -> None:
        with database.transaction() as connection:
            result = connection.execute("DELETE FROM guitar_profiles WHERE profile_id=?", (profile_id,))
            if result.rowcount != 1:
                raise HTTPException(404, "Profil inconnu")

    @app.post("/api/v1/jobs/text", dependencies=[Depends(require_key)], status_code=202)
    async def create_job(value: TextPresetJobRequest) -> dict:
        if len(value.prompt) > config.policy.max_prompt_chars:
            raise HTTPException(422, "Prompt trop long")
        try:
            job_id = manager.create(value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"job_id": job_id, "status_url": f"/api/v1/jobs/{job_id}"}

    @app.get("/api/v1/jobs", dependencies=[Depends(require_key)])
    def list_jobs(limit: int = 30) -> list[dict]:
        return [value.model_dump(mode="json") for value in manager.list(limit)]

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(require_key)])
    def get_job(job_id: str) -> dict:
        try:
            return manager.get(job_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(404, "Travail inconnu") from exc

    @app.get("/api/v1/artifacts/{artifact_id}/download", dependencies=[Depends(require_key)])
    def download(artifact_id: str) -> FileResponse:
        try:
            path = manager.artifact_path(artifact_id)
        except KeyError as exc:
            raise HTTPException(404, "Artefact inconnu") from exc
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @app.post("/api/v1/artifacts/{artifact_id}/import", dependencies=[Depends(require_key)])
    async def import_artifact(artifact_id: str) -> dict:
        try:
            return {"instance_id": await manager.import_artifact(artifact_id)}
        except (KeyError, ValueError, PiPedalAIError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/artifacts/{artifact_id}/activate", dependencies=[Depends(require_key)])
    async def activate_artifact(artifact_id: str) -> dict:
        try:
            return await manager.activate_artifact(artifact_id)
        except (KeyError, ValueError, PiPedalAIError) as exc:
            raise HTTPException(409, str(exc)) from exc

    web_root = Path(__file__).resolve().parent.parent / "web"
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="PiPedal AI Raspberry Pi authority")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("PIPEDAL_AI_CONFIG", "config/pi.toml")))
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args()
    config = load_pi_config(args.config)
    uvicorn.run(create_app(config), host=config.server.host, port=config.server.port,
                ssl_certfile=str(args.tls_cert) if args.tls_cert else None,
                ssl_keyfile=str(args.tls_key) if args.tls_key else None)


if __name__ == "__main__":
    main()
