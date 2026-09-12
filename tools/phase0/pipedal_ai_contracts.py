#!/usr/bin/env python3
"""Export RTX-safe capabilities and validate PresetSpec/ProposalSet documents."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from pipedal_ai_catalog import InventoryValidationError, open_database, utc_now
from pipedal_ai_inspect import atomic_write_json, sha256_json


CAPABILITY_SCHEMA_VERSION = "pipedal-ai.catalog-capabilities/1.0.0"
PRESET_SCHEMA_VERSION = "pipedal-ai.preset-spec/1.0.0"
PROPOSAL_SCHEMA_VERSION = "pipedal-ai.proposal-set/1.0.0"
MAX_CONTRACT_BYTES = 2 * 1024 * 1024
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INSTANCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")

RESOURCE_KIND_BY_ROLE = {
    "nam_model": "nam",
    "cab_ir": "cab_ir",
    "reverb_ir": "reverb_ir",
}
PLUGIN_RESOURCE_ROLES = {
    "http://two-play.com/plugins/toob-nam": frozenset({"nam_model"}),
    "http://two-play.com/plugins/toob-cab-ir": frozenset({"cab_ir"}),
    "http://two-play.com/plugins/toob-convolution-reverb": frozenset({"reverb_ir"}),
    "http://two-play.com/plugins/toob-convolution-reverb-stereo": frozenset({"reverb_ir"}),
}


class ContractValidationError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractValidationError(message)


def exact_keys(value: Any, expected: set[str], label: str) -> None:
    require(isinstance(value, dict), f"{label} doit être un objet JSON.")
    keys = set(value)
    require(keys == expected, f"Champs invalides pour {label} : attendus {sorted(expected)}, reçus {sorted(keys)}")


def read_contract(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ContractValidationError("Le contrat JSON ne doit pas être un lien symbolique.")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ContractValidationError(f"Contrat inaccessible : {exc}") from exc
    require(stat.S_ISREG(metadata.st_mode), "Le contrat doit être un fichier régulier.")
    require(metadata.st_size <= MAX_CONTRACT_BYTES, "Le contrat dépasse 2 Mio.")
    try:
        value = json.loads(
            path.read_bytes(),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ContractValidationError(f"Valeur JSON non finie interdite : {token}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"Contrat JSON illisible : {exc}") from exc
    require(isinstance(value, dict), "La racine du contrat doit être un objet JSON.")
    return value


def active_catalog(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        """SELECT r.* FROM catalog_state s
           JOIN catalog_revisions r ON r.revision = s.active_revision
           WHERE s.singleton = 1"""
    ).fetchone()
    require(row is not None, "Aucune révision de catalogue active.")
    return row


def catalog_ref(row: sqlite3.Row) -> dict[str, Any]:
    return {"revision": row["revision"], "sha256": row["catalog_sha256"]}


def build_capability_set(connection: sqlite3.Connection) -> dict[str, Any]:
    revision = active_catalog(connection)
    revision_id = revision["revision"]
    plugins: list[dict[str, Any]] = []
    plugin_rows = connection.execute(
        """SELECT * FROM catalog_plugins
           WHERE revision = ? ORDER BY uri""",
        (revision_id,),
    ).fetchall()
    for plugin in plugin_rows:
        port_rows = connection.execute(
            """SELECT * FROM plugin_ports
               WHERE revision = ? AND plugin_id = ? ORDER BY port_index""",
            (revision_id, plugin["plugin_id"]),
        ).fetchall()
        controls = []
        audio_inputs = 0
        audio_outputs = 0
        descriptor = json.loads(plugin["descriptor_json"])
        descriptor_ports = {port["index"]: port for port in descriptor["ports"]}
        for port in port_rows:
            if port["kind"] == "audio":
                audio_inputs += port["direction"] == "input"
                audio_outputs += port["direction"] == "output"
            if port["kind"] == "control" and port["direction"] == "input":
                original = descriptor_ports[port["port_index"]]
                controls.append(
                    {
                        "symbol": port["symbol"],
                        "name": port["name"],
                        "datatype": port["datatype"],
                        "minimum": original["minimum"],
                        "maximum": original["maximum"],
                        "default": original["default"],
                        "properties": json.loads(port["properties_json"]),
                        "scale_points": json.loads(port["scale_points_json"]),
                    }
                )
        plugins.append(
            {
                "plugin_id": plugin["plugin_id"],
                "uri": plugin["uri"],
                "name": plugin["name"],
                "class": plugin["class"],
                "has_latency": None if plugin["has_latency"] is None else bool(plugin["has_latency"]),
                "descriptor_sha256": plugin["descriptor_sha256"],
                "audio_inputs": audio_inputs,
                "audio_outputs": audio_outputs,
                "controls": controls,
            }
        )

    assets = []
    for asset in connection.execute(
        """SELECT asset_id, kind, relative_path, extension, size_bytes, sha256
           FROM catalog_assets WHERE revision = ? ORDER BY asset_id""",
        (revision_id,),
    ):
        assets.append(
            {
                "asset_id": asset["asset_id"],
                "display_name": PurePosixPath(asset["relative_path"]).stem,
                "kind": asset["kind"],
                "extension": asset["extension"],
                "size_bytes": asset["size_bytes"],
                "sha256": asset["sha256"],
            }
        )

    payload = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "catalog": catalog_ref(revision),
        "plugins": plugins,
        "assets": assets,
    }
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "capability_sha256": sha256_json(payload),
        "catalog": payload["catalog"],
        "plugins": plugins,
        "assets": assets,
    }


def validate_catalog_reference(value: Any, active: sqlite3.Row, label: str) -> None:
    exact_keys(value, {"revision", "sha256"}, label)
    require(type(value["revision"]) is int and value["revision"] >= 1, f"Révision invalide dans {label}.")
    require(isinstance(value["sha256"], str) and SHA256_PATTERN.fullmatch(value["sha256"]) is not None, f"SHA-256 invalide dans {label}.")
    require(value["revision"] == active["revision"], "Révision de catalogue périmée ou inactive.")
    require(value["sha256"] == active["catalog_sha256"], "Hash de catalogue périmé ou inconnu.")


def validate_parameter(value: Any, port: sqlite3.Row, original: dict[str, Any]) -> None:
    datatype = port["datatype"]
    label = f"{port['plugin_id']}/{port['symbol']}"
    if datatype == "boolean":
        require(type(value) is bool, f"Le paramètre {label} doit être booléen.")
        numeric = int(value)
    elif datatype in {"integer", "enumeration"}:
        require(type(value) is int, f"Le paramètre {label} doit être entier.")
        numeric = value
        if datatype == "enumeration" and original["scale_points"]:
            allowed = {point["value"] for point in original["scale_points"]}
            require(value in allowed, f"Valeur d'énumération refusée pour {label}.")
    else:
        require(type(value) in {int, float} and math.isfinite(value), f"Le paramètre {label} doit être un nombre fini.")
        numeric = value

    minimum = original["minimum"]
    maximum = original["maximum"]
    default = original["default"]
    if minimum is not None and maximum is not None:
        in_range = minimum <= numeric <= maximum
        require(in_range or numeric == default, f"Paramètre hors plage pour {label}.")


def validate_preset_spec(
    connection: sqlite3.Connection,
    spec: dict[str, Any],
    *,
    expected_catalog: dict[str, Any] | None = None,
) -> None:
    exact_keys(spec, {"schema_version", "catalog", "variant", "name", "description", "chain"}, "PresetSpec")
    require(spec["schema_version"] == PRESET_SCHEMA_VERSION, "Version de PresetSpec refusée.")
    active = active_catalog(connection)
    validate_catalog_reference(spec["catalog"], active, "PresetSpec.catalog")
    if expected_catalog is not None:
        require(spec["catalog"] == expected_catalog, "Le PresetSpec ne référence pas le catalogue du ProposalSet.")
    require(spec["variant"] in {"conservative", "balanced", "bold"}, "Variante de preset refusée.")
    require(isinstance(spec["name"], str) and 1 <= len(spec["name"]) <= 80, "Nom de preset invalide.")
    require(isinstance(spec["description"], str) and len(spec["description"]) <= 500, "Description invalide.")
    require(isinstance(spec["chain"], list) and 1 <= len(spec["chain"]) <= 12, "La chaîne doit contenir de 1 à 12 plugins.")

    instances: set[str] = set()
    for index, step in enumerate(spec["chain"]):
        exact_keys(step, {"instance_id", "plugin_id", "bypass", "parameters", "resources"}, f"chain[{index}]")
        instance_id = step["instance_id"]
        require(isinstance(instance_id, str) and INSTANCE_PATTERN.fullmatch(instance_id) is not None, f"instance_id invalide à l'étape {index}.")
        require(instance_id not in instances, f"instance_id dupliqué : {instance_id}")
        instances.add(instance_id)
        require(type(step["bypass"]) is bool, f"bypass doit être booléen à l'étape {index}.")
        plugin = connection.execute(
            """SELECT * FROM catalog_plugins
               WHERE revision = ? AND plugin_id = ?""",
            (active["revision"], step["plugin_id"]),
        ).fetchone()
        require(plugin is not None, f"Plugin inconnu à l'étape {index} : {step['plugin_id']}")
        descriptor = json.loads(plugin["descriptor_json"])
        descriptor_ports = {port["index"]: port for port in descriptor["ports"]}
        controls = {
            row["symbol"]: row
            for row in connection.execute(
                """SELECT * FROM plugin_ports WHERE revision = ? AND plugin_id = ?
                   AND kind = 'control' AND direction = 'input'""",
                (active["revision"], plugin["plugin_id"]),
            )
        }
        require(isinstance(step["parameters"], dict) and len(step["parameters"]) <= 64, f"Paramètres invalides à l'étape {index}.")
        for symbol, value in step["parameters"].items():
            require(symbol in controls, f"Paramètre inconnu : {plugin['name']}/{symbol}")
            port = controls[symbol]
            validate_parameter(value, port, descriptor_ports[port["port_index"]])

        require(isinstance(step["resources"], list) and len(step["resources"]) <= 4, f"Ressources invalides à l'étape {index}.")
        roles: set[str] = set()
        allowed_roles = PLUGIN_RESOURCE_ROLES.get(plugin["uri"], frozenset())
        for resource_index, resource in enumerate(step["resources"]):
            exact_keys(resource, {"role", "asset_id"}, f"chain[{index}].resources[{resource_index}]")
            role = resource["role"]
            require(role in RESOURCE_KIND_BY_ROLE, f"Rôle de ressource inconnu : {role}")
            require(role in allowed_roles, f"Le plugin {plugin['name']} n'accepte pas le rôle {role}.")
            require(role not in roles, f"Rôle de ressource dupliqué : {role}")
            roles.add(role)
            asset = connection.execute(
                """SELECT kind FROM catalog_assets
                   WHERE revision = ? AND asset_id = ?""",
                (active["revision"], resource["asset_id"]),
            ).fetchone()
            require(asset is not None, f"Ressource inconnue : {resource['asset_id']}")
            require(asset["kind"] == RESOURCE_KIND_BY_ROLE[role], f"Type de ressource incompatible pour {role}.")


def validate_proposal_set(connection: sqlite3.Connection, proposal_set: dict[str, Any]) -> None:
    require(isinstance(proposal_set, dict), "ProposalSet doit être un objet")
    expected = {"schema_version", "request_id", "catalog", "proposals"}
    if "decision_report" in proposal_set:
        require(isinstance(proposal_set["decision_report"], dict), "decision_report doit être un objet")
        expected.add("decision_report")
    exact_keys(proposal_set, expected, "ProposalSet")
    require(proposal_set["schema_version"] == PROPOSAL_SCHEMA_VERSION, "Version de ProposalSet refusée.")
    require(isinstance(proposal_set["request_id"], str) and ID_PATTERN.fullmatch(proposal_set["request_id"]) is not None, "request_id invalide.")
    active = active_catalog(connection)
    validate_catalog_reference(proposal_set["catalog"], active, "ProposalSet.catalog")
    proposals = proposal_set["proposals"]
    require(isinstance(proposals, list) and len(proposals) == 3, "Un ProposalSet doit contenir exactement trois presets.")
    for spec in proposals:
        validate_preset_spec(connection, spec, expected_catalog=proposal_set["catalog"])
    variants = [spec["variant"] for spec in proposals]
    require(variants == ["conservative", "balanced", "bold"], "Les variantes doivent être ordonnées : conservative, balanced, bold.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Contrats versionnés PiPedal AI.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export-capabilities")
    export_parser.add_argument("--database", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--force", action="store_true")
    preset_parser = subparsers.add_parser("validate-preset")
    preset_parser.add_argument("--database", type=Path, required=True)
    preset_parser.add_argument("--input", type=Path, required=True)
    proposals_parser = subparsers.add_parser("validate-proposals")
    proposals_parser.add_argument("--database", type=Path, required=True)
    proposals_parser.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        connection = open_database(args.database)
        try:
            if args.command == "export-capabilities":
                result = build_capability_set(connection)
                atomic_write_json(args.output, result, force=args.force)
                print(f"Capacités écrites : {args.output.absolute()}")
                print(f"Révision : {result['catalog']['revision']}")
                print(f"Plugins : {len(result['plugins'])}")
                print(f"Ressources : {len(result['assets'])}")
                print(f"SHA-256 des capacités : {result['capability_sha256']}")
            elif args.command == "validate-preset":
                validate_preset_spec(connection, read_contract(args.input))
                print("PresetSpec valide.")
            else:
                validate_proposal_set(connection, read_contract(args.input))
                print("ProposalSet valide : conservative, balanced, bold.")
        finally:
            connection.close()
    except (ContractValidationError, InventoryValidationError, sqlite3.Error, OSError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
