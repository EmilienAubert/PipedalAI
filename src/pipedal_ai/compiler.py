from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .catalog import CatalogService
from .errors import CompilationError
from .models import PresetSpec


ATOM_PATH = "http://lv2plug.in/ns/ext/atom#Path"
RESOURCE_PROPERTIES = {
    ("http://two-play.com/plugins/toob-nam", "nam_model"):
        "http://two-play.com/plugins/toob-nam#modelFile",
    ("http://two-play.com/plugins/toob-cab-ir", "cab_ir"):
        "http://two-play.com/plugins/toob-cab-ir#impulseFile",
    ("http://two-play.com/plugins/toob-convolution-reverb", "reverb_ir"):
        "http://two-play.com/plugins/toob-impulse#impulseFile",
    ("http://two-play.com/plugins/toob-convolution-reverb-stereo", "reverb_ir"):
        "http://two-play.com/plugins/toob-impulse#impulseFile",
}
TRUSTED_FACTORY_ROOTS = (
    Path("/usr/lib/lv2/ToobAmp.lv2/impulseFiles/CabIR"),
    Path("/usr/lib/lv2/ToobAmp.lv2/impulseFiles/reverb"),
)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return (cleaned[:72] or "preset") + ".piPreset"


class PresetCompiler:
    def __init__(
        self,
        catalog: CatalogService,
        upload_root: Path,
        max_uncompressed_bytes: int,
        input_volume_db: float = -6.0,
        output_volume_db: float = -6.0,
    ):
        self.catalog = catalog
        self.upload_root = upload_root.resolve(strict=False)
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.input_volume_db = min(0.0, max(-24.0, input_volume_db))
        self.output_volume_db = min(0.0, max(-24.0, output_volume_db))

    def compile(self, spec: PresetSpec, output_directory: Path) -> dict[str, Any]:
        self.catalog.validate_preset(spec)
        if output_directory.is_symlink():
            raise CompilationError("Le dossier d'artefacts ne doit pas être un lien symbolique.")
        output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_path = output_directory / _safe_filename(f"{spec.variant}-{spec.name}")
        if output_path.exists() or output_path.is_symlink():
            raise CompilationError(f"L'artefact existe déjà : {output_path.name}")

        items = []
        media: dict[str, bytes] = {}
        used_plugins: dict[str, dict[str, Any]] = {}
        for instance_number, step in enumerate(spec.chain, start=1):
            plugin = self.catalog.plugin_row(spec.catalog.revision, step.plugin_id)
            descriptor = json.loads(plugin["descriptor_json"])
            controls: dict[str, int | float] = {}
            for port in descriptor["ports"]:
                if port["kind"] == "control" and port["default"] is not None:
                    controls[port["symbol"]] = port["default"]
            for symbol, value in step.parameters.items():
                controls[symbol] = int(value) if type(value) is bool else value

            state_values: dict[str, Any] = {}
            path_properties: dict[str, str] = {}
            for binding in step.resources:
                asset = self.catalog.asset_row(spec.catalog.revision, binding.asset_id)
                property_uri = RESOURCE_PROPERTIES.get((plugin["uri"], binding.role))
                if property_uri is None:
                    raise CompilationError(f"Aucun adaptateur de ressource pour {plugin['name']}/{binding.role}")
                relative_path = asset["relative_path"]
                content = self._verified_asset(relative_path, asset["size_bytes"], asset["sha256"])
                media[f"media/{relative_path}"] = content
                state_values[property_uri] = {
                    "flags": 3,
                    "atomType": ATOM_PATH,
                    "value": relative_path,
                }
                path_properties[property_uri] = json.dumps(
                    {"otype_": "Path", "value": relative_path},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            items.append(
                {
                    "instanceId": instance_number,
                    "uri": plugin["uri"],
                    "isEnabled": not step.bypass,
                    "controlValues": [
                        {"key": symbol, "value": value}
                        for symbol, value in sorted(controls.items())
                    ],
                    "pluginName": plugin["name"] or plugin["uri"],
                    "midiBindings": [],
                    "midiChannelBinding": None,
                    "stateUpdateCount": 1 if state_values else 0,
                    "lv2State": [bool(state_values), state_values],
                    "lilvPresetUri": "",
                    "pathProperties": path_properties,
                    "title": "",
                    "useModUi": False,
                    "iconColor": "",
                    "sideChainInputId": -1,
                }
            )
            used_plugins[plugin["uri"]] = {
                "authorName": plugin["author"] or "",
                "name": plugin["name"] or plugin["uri"],
                "uri": plugin["uri"],
            }

        bank = {
            "name": spec.name,
            "nextInstanceId": 2,
            "selectedPreset": 1,
            "presets": [
                {
                    "instanceId": 1,
                    "preset": {
                        "name": spec.name,
                        "input_volume_db": self.input_volume_db,
                        "output_volume_db": self.output_volume_db,
                        "items": items,
                        "nextInstanceId": len(items) + 1,
                        "snapshots": [],
                        "selectedSnapshot": -1,
                        "selectedPlugin": items[0]["instanceId"] if items else -1,
                    },
                }
            ],
        }
        entries = {
            "bankFile.json": _json_bytes(bank),
            "pluginsUsed.json": _json_bytes(
                [used_plugins[uri] for uri in sorted(used_plugins)]
            ),
            **media,
        }
        total = sum(len(content) for content in entries.values())
        if total > self.max_uncompressed_bytes:
            raise CompilationError("Le preset dépasse la limite décompressée.")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=output_directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with zipfile.ZipFile(temporary, "w") as archive:
                for name in sorted(entries):
                    archive.writestr(_zip_info(name), entries[name])
            self.validate_archive(temporary, expected_media=media)
            os.replace(temporary, output_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        content = output_path.read_bytes()
        return {
            "path": str(output_path),
            "filename": output_path.name,
            "sha256": _hash_bytes(content),
            "size_bytes": len(content),
            "variant": spec.variant,
            "preset_name": spec.name,
        }

    def _verified_asset(self, relative_path: str, size_bytes: int, expected_hash: str) -> bytes:
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative_path or "\x00" in relative_path:
            raise CompilationError("Chemin de ressource non sûr.")
        logical = self.upload_root / Path(*pure.parts)
        if not logical.is_file():
            raise CompilationError(f"Ressource locale absente : {relative_path}")
        resolved = logical.resolve(strict=True)
        allowed_roots = [self.upload_root]
        for candidate in TRUSTED_FACTORY_ROOTS:
            try:
                allowed_roots.append(candidate.resolve(strict=True))
            except OSError:
                pass
        if not any(_is_within(resolved, root) for root in allowed_roots):
            raise CompilationError(f"Cible de ressource refusée : {relative_path}")
        before = resolved.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size != size_bytes:
            raise CompilationError(f"Taille de ressource modifiée : {relative_path}")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        with resolved.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                chunks.append(block)
        after = resolved.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise CompilationError(f"Ressource modifiée pendant la lecture : {relative_path}")
        if digest.hexdigest() != expected_hash:
            raise CompilationError(f"Hash de ressource périmé : {relative_path}")
        return b"".join(chunks)

    def validate_archive(self, path: Path, expected_media: dict[str, bytes] | None = None) -> None:
        expected_media = expected_media or {}
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise CompilationError("Entrée ZIP dupliquée.")
            if "bankFile.json" not in names or "pluginsUsed.json" not in names:
                raise CompilationError("Structure .piPreset incomplète.")
            total = 0
            for info in archive.infolist():
                pure = PurePosixPath(info.filename)
                if (pure.is_absolute() or ".." in pure.parts or "\\" in info.filename
                        or "\x00" in info.filename or info.is_dir()
                        or stat.S_ISLNK(info.external_attr >> 16)):
                    raise CompilationError("Chemin ZIP refusé.")
                if info.filename not in {"bankFile.json", "pluginsUsed.json"} and not info.filename.startswith("media/"):
                    raise CompilationError("Entrée ZIP inattendue.")
                total += info.file_size
                if total > self.max_uncompressed_bytes:
                    raise CompilationError("Archive décompressée trop grande.")
            bank = json.loads(archive.read("bankFile.json"))
            if not isinstance(bank.get("presets"), list) or len(bank["presets"]) != 1:
                raise CompilationError("BankFile invalide.")
            json.loads(archive.read("pluginsUsed.json"))
            for name, content in expected_media.items():
                if name not in names or _hash_bytes(archive.read(name)) != _hash_bytes(content):
                    raise CompilationError(f"Média ZIP invalide : {name}")
