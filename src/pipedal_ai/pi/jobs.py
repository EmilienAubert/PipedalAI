from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..catalog import CatalogService
from ..compiler import PresetCompiler
from ..db import Database
from ..degraded import DegradedProposer
from ..errors import ContractError, RemoteServiceError
from ..models import CatalogRef, GuitarProfile, JobView, ProposalSet, RTXProposalRequest, TextPresetJobRequest
from .pipedal_client import PiPedalClient
from .rtx_client import RTXClient
from .system_guard import SystemGuard


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class JobManager:
    def __init__(
        self, database: Database, catalog: CatalogService, compiler: PresetCompiler,
        rtx: RTXClient, pipedal: PiPedalClient, guard: SystemGuard, artifact_root: Path,
    ):
        self.database = database
        self.catalog = catalog
        self.compiler = compiler
        self.rtx = rtx
        self.pipedal = pipedal
        self.guard = guard
        self.artifact_root = artifact_root.absolute()
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(1)
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE jobs SET status='failed', source='none',
                   error='Travail interrompu par un redémarrage du service.', updated_at=?
                   WHERE status IN ('queued','running')""",
                (now(),),
            )

    def create(self, request: TextPresetJobRequest) -> str:
        if len(self._tasks) >= 32:
            raise ValueError("La file de travaux est pleine; réessaie plus tard.")
        catalog = self.catalog.active_ref()
        job_id = self.database.new_id("job")
        timestamp = now()
        with self.database.transaction() as connection:
            if request.profile_id:
                found = connection.execute(
                    "SELECT 1 FROM guitar_profiles WHERE profile_id=?", (request.profile_id,)
                ).fetchone()
                if found is None:
                    raise ValueError("Profil de guitare inconnu.")
            connection.execute(
                """INSERT INTO jobs(job_id,kind,status,source,prompt,profile_id,catalog_revision,
                   catalog_sha256,request_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, "text", "queued", "none", request.prompt, request.profile_id,
                 catalog.revision, catalog.sha256, self.database.json(request.model_dump(mode="json")),
                 timestamp, timestamp),
            )
        task = asyncio.create_task(self._run(job_id, request, catalog))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def _run(self, job_id: str, request: TextPresetJobRequest, catalog_ref: CatalogRef) -> None:
        try:
            async with self._semaphore:
                self._set_job(job_id, status="running")
                self.guard.admit_background_job()
                self._require_active(catalog_ref)
                capabilities = self.catalog.capabilities(catalog_ref)
                profile = self._profile(request.profile_id)
                rtx_request = RTXProposalRequest(
                    schema_version="pipedal-ai.rtx-request/1.0.0", request_id=job_id,
                    prompt=request.prompt, profile=profile, capabilities=capabilities,
                )
                try:
                    proposal = await self.rtx.propose(rtx_request)
                    source = "rtx"
                except RemoteServiceError:
                    proposal = DegradedProposer().propose(job_id, request.prompt, capabilities, profile)
                    source = "degraded"

                self._require_active(catalog_ref)
                try:
                    self._validate_correlated(proposal, job_id, catalog_ref)
                    self.catalog.validate_proposal_set(proposal)
                except ContractError:
                    if source != "rtx":
                        raise
                    proposal = DegradedProposer().propose(job_id, request.prompt, capabilities, profile)
                    source = "degraded"
                    self._validate_correlated(proposal, job_id, catalog_ref)
                    self.catalog.validate_proposal_set(proposal)

                self._require_active(catalog_ref)
                target = self.artifact_root / job_id
                compiled: list[tuple[Any, dict[str, Any], str]] = []
                for spec in proposal.proposals:
                    artifact = self.compiler.compile(spec, target)
                    artifact_id = self.database.new_id("art")
                    with self.database.transaction() as connection:
                        connection.execute(
                            """INSERT INTO artifacts(artifact_id,job_id,variant,preset_name,path,sha256,
                               size_bytes,imported_instance_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (artifact_id, job_id, spec.variant, spec.name, artifact["path"], artifact["sha256"],
                             artifact["size_bytes"], None, now()),
                        )
                    compiled.append((spec, artifact, artifact_id))

                imported: dict[str, int] = {}
                if request.auto_import:
                    for spec, artifact, artifact_id in compiled:
                        instance_id = await self.pipedal.import_preset(Path(artifact["path"]))
                        imported[spec.variant] = instance_id
                        with self.database.transaction() as connection:
                            connection.execute(
                                "UPDATE artifacts SET imported_instance_id=? WHERE artifact_id=?",
                                (instance_id, artifact_id),
                            )
                if request.auto_activate:
                    chosen = imported["balanced"]
                    await self.pipedal.activate(chosen)
                    with self.database.transaction() as connection:
                        connection.execute(
                            "UPDATE artifacts SET activated=1 WHERE job_id=? AND variant='balanced'", (job_id,)
                        )
                self._set_job(job_id, status="completed", source=source,
                              proposal_json=self.database.json(proposal.model_dump(mode="json")))
        except asyncio.CancelledError:
            self._set_job(job_id, status="failed", error="Travail annulé par l'arrêt du service.")
            raise
        except Exception as exc:
            self._set_job(job_id, status="failed", error=str(exc)[:2000])

    def _require_active(self, expected: CatalogRef) -> None:
        if self.catalog.active_ref() != expected:
            raise ContractError("Le catalogue a changé pendant le travail; relance la génération.")

    @staticmethod
    def _validate_correlated(proposal: ProposalSet, job_id: str, catalog: CatalogRef) -> None:
        if proposal.request_id != job_id:
            raise ContractError("La réponse ne correspond pas au travail demandé.")
        if proposal.catalog != catalog:
            raise ContractError("La réponse ne correspond pas au catalogue figé.")

    def _set_job(self, job_id: str, **values: Any) -> None:
        allowed = {"status", "source", "proposal_json", "error"}
        values = {key: value for key, value in values.items() if key in allowed}
        values["updated_at"] = now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id=?", (*values.values(), job_id)
            )

    def _profile(self, profile_id: str | None) -> dict | None:
        if not profile_id:
            return None
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM guitar_profiles WHERE profile_id=?", (profile_id,)).fetchone()
        return dict(row) if row else None

    def get(self, job_id: str) -> JobView:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            artifacts = [dict(item) for item in connection.execute(
                """SELECT artifact_id,variant,preset_name,sha256,size_bytes,imported_instance_id,
                   activated,created_at FROM artifacts WHERE job_id=? ORDER BY
                   CASE variant WHEN 'conservative' THEN 1 WHEN 'balanced' THEN 2 ELSE 3 END""", (job_id,)
            )]
        return JobView(
            job_id=row["job_id"], status=row["status"], source=row["source"], prompt=row["prompt"],
            catalog={"revision": row["catalog_revision"], "sha256": row["catalog_sha256"]},
            created_at=row["created_at"], updated_at=row["updated_at"], error=row["error"], artifacts=artifacts,
        )

    def list(self, limit: int = 30) -> list[JobView]:
        with self.database.connect() as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT ?", (min(limit, 100),)
            )]
        return [self.get(job_id) for job_id in ids]

    def artifact_path(self, artifact_id: str) -> Path:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT path,sha256,size_bytes FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        path = Path(row["path"])
        if path.is_symlink():
            raise ValueError("L'artefact ne doit pas être un lien symbolique.")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.artifact_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ValueError("Chemin d'artefact invalide.") from exc
        if not resolved.is_file() or resolved.stat().st_size != row["size_bytes"]:
            raise ValueError("Taille d'artefact invalide.")
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise ValueError("Empreinte d'artefact invalide.")
        return resolved

    async def import_artifact(self, artifact_id: str) -> int:
        path = self.artifact_path(artifact_id)
        instance_id = await self.pipedal.import_preset(path)
        with self.database.transaction() as connection:
            connection.execute("UPDATE artifacts SET imported_instance_id=? WHERE artifact_id=?",
                               (instance_id, artifact_id))
        return instance_id

    async def activate_artifact(self, artifact_id: str) -> dict[str, int]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT imported_instance_id FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None or row["imported_instance_id"] is None:
            raise ValueError("L'artefact doit d'abord être importé.")
        result = await self.pipedal.activate(int(row["imported_instance_id"]))
        with self.database.transaction() as connection:
            connection.execute("UPDATE artifacts SET activated=0")
            connection.execute("UPDATE artifacts SET activated=1 WHERE artifact_id=?", (artifact_id,))
        return result
