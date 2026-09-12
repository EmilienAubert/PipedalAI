from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from pydantic import Field
from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from ..di import DIManifest, ingest_di
from ..models import StrictModel
from ..errors import PiPedalAIError
from ..preferences import Feedback, record_feedback
from .bench import BenchService
from .renderer import PiPedalRenderer


class EvaluateRequest(StrictModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    set_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    di_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    maintenance_confirmed: bool = False
    optimize: bool = True


class UploadedDI(StrictModel):
    di_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    audio_base64: str = Field(max_length=44 * 1024 * 1024)


class DIUpload(StrictModel):
    manifest: DIManifest
    files: list[UploadedDI] = Field(min_length=1, max_length=32)


def install_bench_routes(app, config, database, catalog, compiler, pipedal, rtx, guard, manager, require_key):
    bench = BenchService(database, catalog, compiler,
                         PiPedalRenderer(catalog, compiler, pipedal, config.bench, guard), rtx, config.bench)
    app.state.bench = bench
    tasks = set()
    with database.transaction() as c:
        c.execute("UPDATE bench_sessions SET status='failed',error='Séance interrompue; vérifier les journaux restore.json avant reprise' WHERE status='running'")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        pending = list(tasks | manager._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    app.router.lifespan_context = lifespan

    @app.get("/api/v1/di", dependencies=[Depends(require_key)])
    def di_sets():
        with database.connect() as c:
            return [{"set_id": r["set_id"], "sha256": r["sha256"],
                     "files": json.loads(r["manifest_json"])["manifest"]["files"]} for r in c.execute("SELECT * FROM di_sets ORDER BY created_at DESC")]

    @app.post("/api/v1/di/import", dependencies=[Depends(require_key)], status_code=201)
    def upload(value: DIUpload):
        try:
            records = {f.di_id: f for f in value.files}
            if len(records) != len(value.files) or set(records) != {f.di_id for f in value.manifest.files}:
                raise ValueError("Fichiers/manifeste DI incohérents")
            config.bench.di_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if config.bench.di_root.is_symlink():
                raise ValueError("Racine DI invalide")
            with tempfile.TemporaryDirectory(dir=config.bench.di_root) as directory:
                folder = Path(directory)
                total = 0
                manifest = value.manifest.model_copy(deep=True)
                for item in manifest.files:
                    content = base64.b64decode(records[item.di_id].audio_base64, validate=True)
                    total += len(content)
                    if total > 32 * 1024 * 1024:
                        raise ValueError("Lot DI trop volumineux; importer par la commande di-import")
                    item.file = item.di_id + ".wav"
                    (folder / item.file).write_bytes(content)
                path = folder / "manifest.json"
                path.write_text(manifest.model_dump_json(), encoding="utf-8")
                return ingest_di(database, path, config.bench.di_root)
        except (ValueError, OSError, PiPedalAIError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/feedback", dependencies=[Depends(require_key)], status_code=201)
    def feedback(value: Feedback):
        try:
            return record_feedback(database, catalog, value)
        except (ValueError, PiPedalAIError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/bench/evaluate", dependencies=[Depends(require_key)], status_code=202)
    async def evaluate(value: EvaluateRequest):
        if not config.bench.enabled or not value.maintenance_confirmed:
            raise HTTPException(409, "Activer le banc et confirmer une séance hors live")
        if tasks:
            raise HTTPException(409, "Une séance est déjà en cours")
        try:
            bench.di_file(value.set_id, value.di_id)
            job = manager.get(value.job_id)
            if job.status != "completed":
                raise ValueError("Travail terminé requis")
            session = bench.start_session(value.set_id, value.job_id)
        except (KeyError, ValueError, PiPedalAIError) as exc:
            raise HTTPException(422, str(exc)) from exc
        async def run():
            try:
                async with manager._semaphore:
                    await bench.evaluate_job(value.job_id, value.set_id, di_id=value.di_id,
                        maintenance_confirmed=True, optimize=value.optimize, session=session)
            except BaseException as exc:
                bench.finish(session[0], error=str(exc) or type(exc).__name__)
        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"session_id": session[0], "status_url": "/api/v1/bench/" + session[0]}

    @app.get("/api/v1/bench", dependencies=[Depends(require_key)])
    def sessions():
        return bench.sessions()

    @app.get("/api/v1/bench/{session_id}", dependencies=[Depends(require_key)])
    def session(session_id: str):
        try:
            return bench.session(session_id)
        except KeyError as exc:
            raise HTTPException(404, "Séance inconnue") from exc

    @app.get("/api/v1/bench/previews/{candidate_id}", dependencies=[Depends(require_key)])
    def preview(candidate_id: str):
        try:
            return FileResponse(bench.preview(candidate_id), media_type="audio/wav")
        except (KeyError, ValueError, PiPedalAIError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/bench/{session_id}/presets/{variant}", dependencies=[Depends(require_key)])
    def preset(session_id: str, variant: str):
        try:
            path = bench.artifact(session_id, variant)
            return FileResponse(path, media_type="application/zip", filename=path.name)
        except (KeyError, ValueError, PiPedalAIError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/bench/{session_id}/presets/{variant}/import", dependencies=[Depends(require_key)])
    async def import_preset(session_id: str, variant: str):
        if tasks:
            raise HTTPException(409, "Attendre la fin du banc")
        try:
            return {"instance_id": await pipedal.import_preset(bench.artifact(session_id, variant))}
        except (KeyError, ValueError, PiPedalAIError) as exc:
            raise HTTPException(409, str(exc)) from exc
