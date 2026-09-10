from __future__ import annotations

import argparse
import json
from pathlib import Path

from .catalog import CatalogService
from .compiler import PresetCompiler
from .config import load_pi_config
from .db import Database
from .models import PresetSpec, ProposalSet


def _services(config_path: Path):
    config = load_pi_config(config_path)
    database = Database(config.storage.database)
    database.initialize()
    catalog = CatalogService(database, config.policy.max_chain_length)
    compiler = PresetCompiler(catalog, config.storage.upload_root,
                              config.policy.max_artifact_uncompressed_bytes,
                              config.policy.default_input_volume_db,
                              config.policy.default_output_volume_db)
    return config, database, catalog, compiler


def main() -> None:
    parser = argparse.ArgumentParser(prog="pipedal-ai")
    parser.add_argument("--config", type=Path, default=Path("config/pi.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    capability = commands.add_parser("capabilities")
    capability.add_argument("--output", type=Path)
    validate = commands.add_parser("validate-proposals")
    validate.add_argument("file", type=Path)
    compile_cmd = commands.add_parser("compile")
    compile_cmd.add_argument("file", type=Path)
    compile_cmd.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-preset")
    verify.add_argument("file", type=Path)
    args = parser.parse_args()
    _, database, catalog, compiler = _services(args.config)
    if args.command == "status":
        row = database.active_catalog()
        print(json.dumps(dict(row), ensure_ascii=False, indent=2))
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


if __name__ == "__main__":
    main()
