from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .catalog import CatalogService
from .compiler import PresetCompiler
from .config import load_pi_config
from .db import Database
from .models import PresetSpec, ProposalSet, RTXProposalRequest
from .errors import PiPedalAIError
from .pi.rtx_client import RTXClient
from .rtx.tone3000 import Tone3000Client, merge_tone3000_metadata


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _tone3000_metadata(token: str, tone_id: int, model_id: int) -> dict:
    async with Tone3000Client(token) as client:
        tone = await client.get_tone(tone_id)
        page_number = 1
        model = None
        while True:
            page = await client.list_models(tone_id, page=page_number, page_size=300)
            model = next((candidate for candidate in page.data if candidate.id == model_id), None)
            if model is not None or page_number >= page.total_pages:
                break
            page_number += 1
    if model is None:
        raise ValueError("Le modèle Tone3000 n'appartient pas au tone indiqué.")
    enriched = merge_tone3000_metadata(
        {"provenance": {"source": "tone3000", "tone_id": tone_id, "model_id": model_id}},
        tone=tone,
        model=model,
    )
    return enriched["metadata"]["tone3000"]


def _services(config_path: Path):
    config = load_pi_config(config_path)
    database = Database(config.storage.database)
    database.initialize()
    catalog = CatalogService(database, config.policy.max_chain_length)
    compiler = PresetCompiler(catalog, config.storage.upload_root,
                              config.policy.max_artifact_uncompressed_bytes,
                              config.policy.default_input_volume_db,
                              config.policy.default_output_volume_db)
    from .asset_metadata import enrich_local_assets
    enrich_local_assets(catalog, compiler)
    return config, database, catalog, compiler


def main() -> None:
    parser = argparse.ArgumentParser(prog="pipedal-ai")
    parser.add_argument("--config", type=Path, default=Path("config/pi.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("catalog-enrich-local")
    di_import = commands.add_parser("di-import", help="Valide et copie un jeu de DI 48 kHz/24 bits/mono")
    di_import.add_argument("--manifest", type=Path, required=True)
    commands.add_parser("di-list")
    evaluate = commands.add_parser("bench-evaluate", help="Rendus et optimisation, exclusivement hors live")
    evaluate.add_argument("--job-id", required=True)
    evaluate.add_argument("--set", required=True)
    evaluate.add_argument("--di-id")
    evaluate.add_argument("--maintenance-confirmed", action="store_true")
    evaluate.add_argument("--no-optimize", action="store_true")
    characterize = commands.add_parser("characterize", help="Mesure des NAM avec la même DI à plusieurs niveaux")
    characterize.add_argument("--set", required=True)
    characterize.add_argument("--di-id")
    characterize.add_argument("--nam-limit", type=int, default=3)
    characterize.add_argument("--all", action="store_true", help="Parcourt tous les NAM avec budget et reprise par cache")
    characterize.add_argument("--cab-ir")
    characterize.add_argument("--maintenance-confirmed", action="store_true")
    recover = commands.add_parser("bench-recover", help="Restauration locale explicite après interruption du banc")
    recover.add_argument("--journal", type=Path, required=True)
    commands.add_parser("bench-list")
    capability = commands.add_parser("capabilities")
    capability.add_argument("--output", type=Path)
    validate = commands.add_parser("validate-proposals")
    validate.add_argument("file", type=Path)
    compile_cmd = commands.add_parser("compile")
    compile_cmd.add_argument("file", type=Path)
    compile_cmd.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-preset")
    verify.add_argument("file", type=Path)
    diagnose = commands.add_parser("diagnose-rtx", help="Teste la vraie RTX sans repli local ni import PiPedal.")
    diagnose.add_argument("--prompt", required=True)
    diagnose.add_argument("--output", type=Path, help="Compile également les trois .piPreset dans ce dossier.")
    enrich = commands.add_parser("tone3000-enrich", help="Lie des métadonnées Tone3000 à un asset existant.")
    enrich.add_argument("asset_id")
    enrich.add_argument("--tone-id", type=int, required=True)
    enrich.add_argument("--model-id", type=int, required=True)
    enrich.add_argument("--token-env", default="TONE3000_ACCESS_TOKEN")
    args = parser.parse_args()
    config, database, catalog, compiler = _services(args.config)
    if args.command == "di-import":
        from .di import ingest_di
        print(json.dumps(ingest_di(database, args.manifest, config.bench.di_root), ensure_ascii=False, indent=2))
    elif args.command == "di-list":
        with database.connect() as c:
            print(json.dumps([dict(r) for r in c.execute("SELECT set_id,sha256,created_at FROM di_sets")], indent=2))
    elif args.command in ("bench-evaluate", "characterize", "bench-recover", "bench-list"):
        from .pi.bench import BenchService
        from .pi.renderer import PiPedalRenderer
        from .pi.pipedal_client import PiPedalClient
        from .pi.system_guard import SystemGuard
        guard = SystemGuard(config.storage.artifact_root, config.policy.max_load_per_cpu, config.policy.min_free_mb)
        renderer = PiPedalRenderer(catalog, compiler, PiPedalClient(config.pipedal), config.bench, guard)
        bench = BenchService(database, catalog, compiler, renderer, RTXClient(config.rtx), config.bench)
        if args.command == "bench-list":
            result = bench.sessions()
        elif args.command == "bench-recover":
            result = asyncio.run(renderer.recover(args.journal))
        elif args.command == "bench-evaluate":
            result = asyncio.run(bench.evaluate_job(args.job_id, args.set, di_id=args.di_id,
                maintenance_confirmed=args.maintenance_confirmed, optimize=not args.no_optimize))
        else:
            result = asyncio.run(bench.characterize(args.set, nam_limit=10000 if args.all else args.nam_limit, cab_ir_id=args.cab_ir,
                di_id=args.di_id, maintenance_confirmed=args.maintenance_confirmed))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "status":
        row = database.active_catalog()
        print(json.dumps(dict(row), ensure_ascii=False, indent=2))
    elif args.command == "catalog-enrich-local":
        from .asset_metadata import enrich_local_assets
        print(json.dumps(enrich_local_assets(catalog, compiler), ensure_ascii=False, indent=2))
    elif args.command == "capabilities":
        value = json.dumps(catalog.capabilities(), ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(value, encoding="utf-8")
        else:
            print(value, end="")
    elif args.command == "validate-proposals":
        proposal = ProposalSet.model_validate_json(args.file.read_text(encoding="utf-8"))
        catalog.validate_proposal_set(proposal)
        print("ProposalSet valide pour le catalogue actif.")
    elif args.command == "compile":
        raw = json.loads(args.file.read_text(encoding="utf-8"))
        specs = ProposalSet.model_validate(raw).proposals if "proposals" in raw else [PresetSpec.model_validate(raw)]
        for spec in specs:
            print(json.dumps(compiler.compile(spec, args.output), ensure_ascii=False))
    elif args.command == "verify-preset":
        compiler.validate_archive(args.file)
        print("Archive .piPreset structurellement valide.")
    elif args.command == "diagnose-rtx":
        frozen = catalog.active_ref()
        rtx_request = RTXProposalRequest(
            schema_version="pipedal-ai.rtx-request/1.0.0",
            request_id=database.new_id("diagnostic"), prompt=args.prompt,
            capabilities=catalog.capabilities(frozen),
        )
        try:
            proposal = asyncio.run(RTXClient(config.rtx).propose(rtx_request))
            if proposal.request_id != rtx_request.request_id or proposal.catalog != frozen:
                raise PiPedalAIError("Réponse RTX associée à un autre travail ou catalogue.")
            if catalog.active_ref() != frozen:
                raise PiPedalAIError("Le catalogue a changé pendant le diagnostic.")
            catalog.validate_proposal_set(proposal)
            artifacts = [compiler.compile(spec, args.output) for spec in proposal.proposals] if args.output else []
            if args.output:
                from .audio_io import atomic_json
                atomic_json(args.output / "proposals.json", proposal.model_dump(mode="json"))
        except PiPedalAIError as exc:
            raise SystemExit(f"Diagnostic RTX échoué (aucun repli local) : {exc}") from exc
        print(json.dumps({"source": "rtx", "catalog": frozen.model_dump(mode="json"),
                          "variants": [spec.variant for spec in proposal.proposals],
                          "artifacts": artifacts, "imported": False}, ensure_ascii=False, indent=2))
    elif args.command == "tone3000-enrich":
        token = os.environ.get(args.token_env, "").strip()
        if not token:
            raise SystemExit(f"La variable {args.token_env} est requise.")
        active = catalog.active_ref()
        catalog.asset_row(active.revision, args.asset_id)
        metadata = asyncio.run(_tone3000_metadata(token, args.tone_id, args.model_id))
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO asset_metadata(revision,asset_id,source,metadata_json,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(revision,asset_id,source)
                   DO UPDATE SET metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                (active.revision, args.asset_id, "tone3000", database.json(metadata), _now()),
            )
        print(json.dumps({"asset_id": args.asset_id, "catalog": active.model_dump(),
                          "tone3000": metadata}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
