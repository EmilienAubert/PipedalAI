#!/usr/bin/env python3
"""Validate a phase-zero inventory and publish immutable SQLite revisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from pipedal_ai_inspect import (
    CATALOG_SCHEMA_VERSION,
    SCHEMA_VERSION,
    build_catalog_projection,
    canonical_json_bytes,
    plugin_descriptor,
    sha256_json,
    stable_id,
)


DATABASE_SCHEMA_VERSION = 1
MAX_INVENTORY_BYTES = 32 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
KIND_PREFIXES = {
    "nam": "NeuralAmpModels/",
    "cab_ir": "CabIR/",
    "reverb_ir": "ReverbImpulseFiles/",
}


class InventoryValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compact_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def read_inventory(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        raise InventoryValidationError("Le fichier d'inventaire ne doit pas être un lien symbolique.")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise InventoryValidationError(f"Inventaire inaccessible : {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise InventoryValidationError("L'inventaire doit être un fichier régulier.")
    if metadata.st_size > MAX_INVENTORY_BYTES:
        raise InventoryValidationError("L'inventaire dépasse la limite de 32 Mio.")
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw,
            parse_constant=lambda token: (_ for _ in ()).throw(
                InventoryValidationError(f"Valeur JSON non finie interdite : {token}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryValidationError(f"Inventaire JSON illisible : {exc}") from exc
    if not isinstance(value, dict):
        raise InventoryValidationError("La racine de l'inventaire doit être un objet JSON.")
    return value, hashlib.sha256(raw).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InventoryValidationError(message)


def validate_number(value: Any, label: str) -> None:
    if value is None:
        return
    require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        f"{label} doit être un nombre fini ou null.",
    )


def sqlite_number(value: int | float | None) -> float | None:
    """Store LV2 numeric metadata as REAL, including very large sentinels."""
    return None if value is None else float(value)


def validate_inventory(inventory: dict[str, Any]) -> None:
    require(inventory.get("schema_version") == SCHEMA_VERSION, "Version de schéma d'inventaire refusée.")
    require(inventory.get("status") == "complete", "Seul un inventaire complet peut être publié.")
    require(isinstance(inventory.get("collector"), dict), "Bloc collector absent.")
    require(inventory["collector"].get("network_access") is False, "Le collecteur déclare un accès réseau.")
    require(inventory["collector"].get("shell_execution") is False, "Le collecteur déclare une exécution shell.")
    require(isinstance(inventory.get("lv2"), dict), "Bloc LV2 absent.")
    require(inventory["lv2"].get("status") == "complete", "Inventaire LV2 incomplet.")
    require(isinstance(inventory.get("assets"), dict), "Bloc assets absent.")
    require(isinstance(inventory.get("catalog"), dict), "Bloc catalog absent.")
    require(inventory["catalog"].get("schema_version") == CATALOG_SCHEMA_VERSION, "Version du catalogue refusée.")
    require(inventory["catalog"].get("version") is None, "L'inventaire source ne doit pas attribuer sa propre révision.")

    plugins = inventory["lv2"].get("plugins")
    assets = inventory["assets"].get("items")
    warnings = inventory.get("warnings")
    require(isinstance(plugins, list), "Liste de plugins absente.")
    require(isinstance(assets, list), "Liste de ressources absente.")
    require(isinstance(warnings, list), "Liste d'avertissements absente.")
    require(not any(item.get("severity") == "error" for item in warnings), "Un inventaire complet ne peut contenir d'erreur.")

    plugin_ids: set[str] = set()
    plugin_uris: set[str] = set()
    for plugin in plugins:
        require(isinstance(plugin, dict), "Descripteur de plugin invalide.")
        uri = plugin.get("uri")
        require(isinstance(uri, str) and uri, "URI de plugin absente.")
        require(plugin.get("reported_uri") == uri, f"URI LV2 discordante : {uri}")
        expected_id = stable_id("plg", uri)
        require(plugin.get("plugin_id") == expected_id, f"Identifiant de plugin invalide : {uri}")
        require(expected_id not in plugin_ids and uri not in plugin_uris, f"Plugin dupliqué : {uri}")
        plugin_ids.add(expected_id)
        plugin_uris.add(uri)
        require(plugin.get("descriptor_scope") == "lv2info-v1", f"Portée de descripteur inconnue : {uri}")
        expected_descriptor_hash = sha256_json(plugin_descriptor(plugin))
        require(
            plugin.get("descriptor_sha256") == expected_descriptor_hash,
            f"Hash de descripteur invalide : {uri}",
        )

        ports = plugin.get("ports")
        require(isinstance(ports, list) and ports, f"Aucun port pour le plugin : {uri}")
        indexes: set[int] = set()
        symbols: set[str] = set()
        for port in ports:
            require(isinstance(port, dict), f"Port invalide : {uri}")
            index = port.get("index")
            symbol = port.get("symbol")
            require(isinstance(index, int) and not isinstance(index, bool) and index >= 0, f"Index de port invalide : {uri}")
            require(isinstance(symbol, str) and symbol, f"Symbole de port absent : {uri}/{index}")
            require(index not in indexes, f"Index de port dupliqué : {uri}/{index}")
            require(symbol not in symbols, f"Symbole de port dupliqué : {uri}/{symbol}")
            indexes.add(index)
            symbols.add(symbol)
            for key in ("minimum", "maximum", "default"):
                validate_number(port.get(key), f"{uri}/{symbol}/{key}")
            minimum = port.get("minimum")
            maximum = port.get("maximum")
            if minimum is not None and maximum is not None:
                require(minimum <= maximum, f"Plage de port inversée : {uri}/{symbol}")

    asset_ids: set[str] = set()
    asset_paths: set[str] = set()
    for asset in assets:
        require(isinstance(asset, dict), "Descripteur de ressource invalide.")
        kind = asset.get("kind")
        relative_path = asset.get("relative_path")
        digest = asset.get("sha256")
        require(kind in KIND_PREFIXES, f"Type de ressource refusé : {kind}")
        require(isinstance(relative_path, str) and relative_path, "Chemin de ressource absent.")
        pure_path = PurePosixPath(relative_path)
        require(not pure_path.is_absolute() and ".." not in pure_path.parts, f"Chemin de ressource refusé : {relative_path}")
        require(relative_path.startswith(KIND_PREFIXES[kind]), f"Racine de ressource incorrecte : {relative_path}")
        require(isinstance(digest, str) and SHA256_PATTERN.fullmatch(digest) is not None, f"SHA-256 de ressource invalide : {relative_path}")
        require(isinstance(asset.get("size_bytes"), int) and asset["size_bytes"] >= 0, f"Taille de ressource invalide : {relative_path}")
        expected_id = stable_id("ast", kind, relative_path, digest)
        require(asset.get("asset_id") == expected_id, f"Identifiant de ressource invalide : {relative_path}")
        require(expected_id not in asset_ids and relative_path not in asset_paths, f"Ressource dupliquée : {relative_path}")
        asset_ids.add(expected_id)
        asset_paths.add(relative_path)

    require(inventory["lv2"].get("plugin_count") == len(plugins), "Compteur LV2 discordant.")
    require(inventory["assets"].get("asset_count") == len(assets), "Compteur de ressources discordant.")
    require(inventory["catalog"].get("plugin_count") == len(plugins), "Compteur de plugins du catalogue discordant.")
    require(inventory["catalog"].get("asset_count") == len(assets), "Compteur d'assets du catalogue discordant.")
    actual_catalog_hash = sha256_json(build_catalog_projection(inventory["lv2"], inventory["assets"]))
    require(inventory["catalog"].get("sha256") == actual_catalog_hash, "Hash du catalogue discordant.")


def open_database(path: Path) -> sqlite3.Connection:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise InventoryValidationError("La base SQLite ne doit pas être un lien symbolique.")
    require(path.parent.is_dir(), f"Le dossier de la base n'existe pas : {path.parent}")
    existed = path.exists()
    if existed:
        require(path.is_file(), "Le chemin SQLite existant n'est pas un fichier régulier.")
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        require(
            user_version in (0, DATABASE_SCHEMA_VERSION, 2),
            "Version de schéma SQLite incompatible.",
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        schema_path = Path(__file__).resolve().parent / "schemas" / "catalog-v1.sql"
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        if user_version == 0:
            connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
        if not existed:
            os.chmod(path, 0o600)
        return connection
    except Exception:
        connection.close()
        raise


def import_inventory(
    connection: sqlite3.Connection,
    inventory: dict[str, Any],
    source_inventory_sha256: str,
) -> dict[str, Any]:
    validate_inventory(inventory)
    catalog_hash = inventory["catalog"]["sha256"]
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT revision FROM catalog_revisions WHERE catalog_sha256 = ?", (catalog_hash,)
        ).fetchone()
        if existing is not None:
            active = connection.execute(
                "SELECT active_revision FROM catalog_state WHERE singleton = 1"
            ).fetchone()
            connection.execute("COMMIT")
            return {
                "created": False,
                "revision": existing["revision"],
                "active_revision": active["active_revision"] if active else None,
                "catalog_sha256": catalog_hash,
            }

        row = connection.execute("SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM catalog_revisions").fetchone()
        revision = row["revision"]
        plugins = inventory["lv2"]["plugins"]
        assets = inventory["assets"]["items"]
        warnings = inventory["warnings"]
        connection.execute(
            """INSERT INTO catalog_revisions (
                   revision, catalog_sha256, catalog_schema_version,
                   inventory_schema_version, source_inventory_sha256,
                   collector_version, generated_at, imported_at,
                   plugin_count, asset_count, warning_count
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                revision,
                catalog_hash,
                inventory["catalog"]["schema_version"],
                inventory["schema_version"],
                source_inventory_sha256,
                inventory["collector"]["version"],
                inventory["generated_at"],
                utc_now(),
                len(plugins),
                len(assets),
                len(warnings),
            ),
        )
        for plugin in plugins:
            connection.execute(
                """INSERT INTO catalog_plugins (
                       revision, plugin_id, uri, name, class, author, has_latency,
                       descriptor_sha256, descriptor_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision,
                    plugin["plugin_id"],
                    plugin["uri"],
                    plugin["name"],
                    plugin["class"],
                    plugin["author"],
                    None if plugin["has_latency"] is None else int(plugin["has_latency"]),
                    plugin["descriptor_sha256"],
                    compact_json(plugin_descriptor(plugin)),
                ),
            )
            for port in plugin["ports"]:
                connection.execute(
                    """INSERT INTO plugin_ports (
                           revision, plugin_id, port_index, symbol, name, direction,
                           kind, datatype, minimum, maximum, default_value,
                           properties_json, scale_points_json, supported_events_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        revision,
                        plugin["plugin_id"],
                        port["index"],
                        port["symbol"],
                        port["name"],
                        port["direction"],
                        port["kind"],
                        port["datatype"],
                        sqlite_number(port["minimum"]),
                        sqlite_number(port["maximum"]),
                        sqlite_number(port["default"]),
                        compact_json(port["properties"]),
                        compact_json(port["scale_points"]),
                        compact_json(port["supported_events"]),
                    ),
                )
        for asset in assets:
            connection.execute(
                """INSERT INTO catalog_assets (
                       revision, asset_id, kind, relative_path, extension,
                       size_bytes, sha256
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision,
                    asset["asset_id"],
                    asset["kind"],
                    asset["relative_path"],
                    asset["extension"],
                    asset["size_bytes"],
                    asset["sha256"],
                ),
            )
        for index, item in enumerate(warnings):
            connection.execute(
                """INSERT INTO catalog_warnings (
                       revision, warning_index, severity, code, warning_json
                   ) VALUES (?, ?, ?, ?, ?)""",
                (revision, index, item["severity"], item["code"], compact_json(item)),
            )
        connection.execute(
            """INSERT INTO catalog_state(singleton, active_revision) VALUES (1, ?)
               ON CONFLICT(singleton) DO UPDATE SET active_revision = excluded.active_revision""",
            (revision,),
        )
        connection.execute("COMMIT")
        return {
            "created": True,
            "revision": revision,
            "active_revision": revision,
            "catalog_sha256": catalog_hash,
        }
    except Exception:
        connection.execute("ROLLBACK")
        raise


def database_status(connection: sqlite3.Connection) -> dict[str, Any]:
    active = connection.execute(
        """SELECT r.* FROM catalog_state s
           JOIN catalog_revisions r ON r.revision = s.active_revision
           WHERE s.singleton = 1"""
    ).fetchone()
    revisions = connection.execute("SELECT COUNT(*) FROM catalog_revisions").fetchone()[0]
    return {"revision_count": revisions, "active": dict(active) if active else None}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Catalogue SQLite autoritaire de PiPedal AI.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import", help="Valider et publier un inventaire.")
    import_parser.add_argument("--inventory", type=Path, required=True)
    import_parser.add_argument("--database", type=Path, required=True)
    status_parser = subparsers.add_parser("status", help="Afficher la révision active.")
    status_parser.add_argument("--database", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        connection = open_database(args.database)
        try:
            if args.command == "import":
                inventory, source_hash = read_inventory(args.inventory)
                result = import_inventory(connection, inventory, source_hash)
                action = "créée" if result["created"] else "déjà présente"
                print(f"Révision {result['revision']} {action}.")
                print(f"Révision active : {result['active_revision']}")
                print(f"SHA-256 du catalogue : {result['catalog_sha256']}")
            else:
                print(json.dumps(database_status(connection), ensure_ascii=False, indent=2))
        finally:
            connection.close()
    except (InventoryValidationError, sqlite3.Error, OSError, OverflowError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
