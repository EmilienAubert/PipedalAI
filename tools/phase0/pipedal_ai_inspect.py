#!/usr/bin/env python3
"""Collect a read-only PiPedal/LV2/NAM/IR inventory.

This phase-zero collector intentionally uses only the Python standard library.
It never invokes a shell, never accesses the network, and writes only the
explicitly requested output file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "pipedal-ai.inventory/1.0.0"
CATALOG_SCHEMA_VERSION = "pipedal-ai.catalog-source/1.0.0"
COLLECTOR_VERSION = "0.2.0"
DEFAULT_DATA_ROOT = Path("/var/pipedal")
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024

ASSET_DIRECTORIES: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("NeuralAmpModels", "nam", frozenset({".nam", ".aidax"})),
    ("CabIR", "cab_ir", frozenset({".wav", ".wave", ".flac", ".aif", ".aiff"})),
    (
        "ReverbImpulseFiles",
        "reverb_ir",
        frozenset({".wav", ".wave", ".flac", ".aif", ".aiff"}),
    ),
)

# PiPedal exposes the factory TooB impulse responses through symbolic links in
# audio_uploads. Only these package-owned roots (and targets that stay inside
# their normal category root) are trusted. Absolute target paths never enter
# the catalog projection sent to the RTX service.
DEFAULT_TRUSTED_SYMLINK_ROOTS: Mapping[str, tuple[Path, ...]] = {
    "nam": (),
    "cab_ir": (Path("/usr/lib/lv2/ToobAmp.lv2/impulseFiles/CabIR"),),
    "reverb_ir": (Path("/usr/lib/lv2/ToobAmp.lv2/impulseFiles/reverb"),),
}

TOP_LIST_FIELDS = {
    "Required Features": "required_features",
    "Optional Features": "optional_features",
    "Extension Data": "extension_data",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def warning(
    warnings: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    severity: str = "warning",
    context: dict[str, Any] | None = None,
) -> None:
    item: dict[str, Any] = {"severity": severity, "code": code, "message": message}
    if context:
        item["context"] = context
    warnings.append(item)


def run_command(argv: Sequence[str], timeout_seconds: float = 20.0) -> dict[str, Any]:
    """Run a fixed argv without a shell and return bounded UTF-8 output."""
    command_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if os.environ.get("LV2_PATH"):
        command_env["LV2_PATH"] = os.environ["LV2_PATH"]
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout_seconds,
            check=False,
            env=command_env,
        )
    except FileNotFoundError:
        return {"status": "missing", "returncode": None, "stdout": "", "stderr": ""}
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "returncode": None,
            "stdout": _decode_bounded(exc.stdout or b""),
            "stderr": _decode_bounded(exc.stderr or b""),
        }
    except OSError as exc:
        return {
            "status": "error",
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }

    stdout_truncated = len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
    stderr_truncated = len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
    stdout = _decode_bounded(completed.stdout)
    stderr = _decode_bounded(completed.stderr)
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def _decode_bounded(data: bytes) -> str:
    if len(data) > MAX_COMMAND_OUTPUT_BYTES:
        data = data[:MAX_COMMAND_OUTPUT_BYTES]
    return data.decode("utf-8", errors="replace")


def parse_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    allowed = {"ID", "VERSION_ID", "PRETTY_NAME"}
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in allowed:
                result[key.lower()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return result


def collect_host() -> dict[str, Any]:
    model: str | None = None
    model_path = Path("/proc/device-tree/model")
    try:
        model = model_path.read_bytes().rstrip(b"\0").decode("utf-8", errors="replace")
    except OSError:
        pass

    return {
        "model": model,
        "machine": platform.machine(),
        "kernel_release": platform.release(),
        "python_version": platform.python_version(),
        "os_release": parse_os_release(),
    }


def collect_pipedal(data_root: Path, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    package = run_command(["dpkg-query", "-W", "-f=${Version}", "pipedal"], 5.0)
    package_version: str | None = None
    if package["status"] == "ok":
        package_version = package["stdout"].strip() or None
    else:
        warning(
            warnings,
            "PIPEDAL_VERSION_UNAVAILABLE",
            "La version du paquet Debian pipedal n'a pas pu être déterminée.",
            context={"command_status": package["status"]},
        )

    service = run_command(["systemctl", "is-active", "pipedald"], 5.0)
    service_state = service["stdout"].strip() or "unknown"
    if service["status"] not in {"ok", "failed"}:
        service_state = "unknown"

    audio_config = collect_audio_config(data_root / "AudioConfig.json", warnings)
    return {
        "package_version": package_version,
        "service_state": service_state,
        "data_root": str(data_root),
        "audio_config": audio_config,
    }


def collect_audio_config(path: Path, warnings: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not path.exists():
        warning(
            warnings,
            "AUDIO_CONFIG_MISSING",
            "AudioConfig.json n'existe pas dans la racine PiPedal indiquée.",
            context={"path": str(path)},
        )
        return None

    try:
        if path.is_symlink():
            raise ValueError("les liens symboliques ne sont pas lus")
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("fichier supérieur à 1 Mio")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        warning(
            warnings,
            "AUDIO_CONFIG_INVALID",
            "AudioConfig.json n'a pas pu être lu.",
            context={"path": str(path), "error": str(exc)},
        )
        return None

    if not isinstance(value, dict):
        warning(warnings, "AUDIO_CONFIG_INVALID", "AudioConfig.json n'est pas un objet JSON.")
        return None

    allowed_keys = (
        "valid",
        "isJackAudio",
        "alsaDevice",
        "alsaInputDevice",
        "alsaOutputDevice",
        "alsaInputDeviceName",
        "alsaOutputDeviceName",
        "sampleRate",
        "bufferSize",
        "numberOfBuffers",
    )
    return {key: value[key] for key in allowed_keys if key in value}


def parse_number(text: str) -> int | float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    if value.is_integer():
        return int(value)
    return value


def _port_direction(types: Iterable[str]) -> str | None:
    values = tuple(types)
    if any(value.endswith("#InputPort") for value in values):
        return "input"
    if any(value.endswith("#OutputPort") for value in values):
        return "output"
    return None


def _port_kind(types: Iterable[str]) -> str:
    values = tuple(types)
    suffixes = (
        ("#ControlPort", "control"),
        ("#AudioPort", "audio"),
        ("#CVPort", "cv"),
        ("#AtomPort", "atom"),
        ("#EventPort", "event"),
    )
    for suffix, name in suffixes:
        if any(value.endswith(suffix) for value in values):
            return name
    return "unknown"


def _port_datatype(properties: Iterable[str]) -> str:
    values = tuple(properties)
    if any(value.endswith("#toggled") for value in values):
        return "boolean"
    if any(value.endswith("#enumeration") for value in values):
        return "enumeration"
    if any(value.endswith("#integer") for value in values):
        return "integer"
    return "number"


def parse_lv2info(raw: str, expected_uri: str) -> dict[str, Any]:
    """Parse the stable, public text format produced by Lilv's lv2info tool."""
    plugin: dict[str, Any] = {
        "uri": expected_uri,
        "name": None,
        "class": None,
        "author": None,
        "has_latency": None,
        "required_features": [],
        "optional_features": [],
        "extension_data": [],
        "ports": [],
    }
    current_port: dict[str, Any] | None = None
    current_section: str | None = None
    first_nonempty: str | None = None

    for original_line in raw.replace("\r\n", "\n").split("\n"):
        stripped = original_line.strip()
        expanded = original_line.expandtabs(8)
        indentation = len(expanded) - len(expanded.lstrip(" "))
        if stripped and first_nonempty is None:
            first_nonempty = stripped
        if not stripped:
            current_section = None
            continue

        port_match = re.fullmatch(r"Port\s+(\d+):", stripped)
        if port_match:
            current_port = {
                "index": int(port_match.group(1)),
                "symbol": None,
                "name": None,
                "types": [],
                "direction": None,
                "kind": "unknown",
                "datatype": "number",
                "minimum": None,
                "maximum": None,
                "default": None,
                "properties": [],
                "scale_points": [],
                "supported_events": [],
            }
            plugin["ports"].append(current_port)
            current_section = None
            continue

        if current_port is None:
            # Lilv prints plugin fields at one tab (8 columns). Nested UI
            # metadata is more deeply indented and can repeat fields such as
            # Class. Reading it as plugin metadata turns almost every plugin
            # into an X11UI. Presets are another nested list and must not leak
            # into Required/Optional Features or Extension Data.
            is_plugin_field = indentation == 8
            matched_top = False
            scalar_fields = {
                "Name": "name",
                "Class": "class",
                "Author": "author",
                "Has latency": "has_latency",
            }
            for label, key in scalar_fields.items():
                prefix = f"{label}:"
                if is_plugin_field and stripped.startswith(prefix):
                    value = stripped[len(prefix) :].strip()
                    plugin[key] = value.startswith("yes") if key == "has_latency" else value
                    current_section = None
                    matched_top = True
                    break
            if matched_top:
                continue

            for label, key in TOP_LIST_FIELDS.items():
                prefix = f"{label}:"
                if is_plugin_field and stripped.startswith(prefix):
                    value = stripped[len(prefix) :].strip()
                    if value:
                        plugin[key].append(value)
                    current_section = key
                    matched_top = True
                    break
            if matched_top:
                continue

            if current_section in TOP_LIST_FIELDS.values() and indentation > 8:
                plugin[current_section].append(stripped)
            elif is_plugin_field:
                # An unhandled top-level field or section (for example UIs,
                # Data URIs or Presets) terminates a preceding list field.
                current_section = None
            continue

        field_prefixes = {
            "Symbol:": "symbol",
            "Name:": "name",
            "Minimum:": "minimum",
            "Maximum:": "maximum",
            "Default:": "default",
        }
        matched_port = False
        for prefix, key in field_prefixes.items():
            if stripped.startswith(prefix):
                value = stripped[len(prefix) :].strip()
                current_port[key] = parse_number(value) if key in {"minimum", "maximum", "default"} else value
                current_section = None
                matched_port = True
                break
        if matched_port:
            continue

        if stripped.startswith("Type:"):
            value = stripped[len("Type:") :].strip()
            if value:
                current_port["types"].append(value)
            current_section = "types"
            continue
        if stripped.startswith("Properties:"):
            value = stripped[len("Properties:") :].strip()
            if value:
                current_port["properties"].append(value)
            current_section = "properties"
            continue
        if stripped == "Scale Points:":
            current_section = "scale_points"
            continue
        if stripped == "Supported events:":
            current_section = "supported_events"
            continue

        if current_section in {"types", "properties", "supported_events"}:
            current_port[current_section].append(stripped)
        elif current_section == "scale_points":
            point_match = re.fullmatch(r'(.+?)\s*=\s*"(.*)"', stripped)
            if point_match:
                value = parse_number(point_match.group(1).strip())
                if value is not None:
                    current_port["scale_points"].append(
                        {"value": value, "label": point_match.group(2)}
                    )

    for port in plugin["ports"]:
        port["types"] = sorted(set(port["types"]))
        port["properties"] = sorted(set(port["properties"]))
        port["supported_events"] = sorted(set(port["supported_events"]))
        port["direction"] = _port_direction(port["types"])
        port["kind"] = _port_kind(port["types"])
        port["datatype"] = _port_datatype(port["properties"])

    plugin["required_features"] = sorted(set(plugin["required_features"]))
    plugin["optional_features"] = sorted(set(plugin["optional_features"]))
    plugin["extension_data"] = sorted(set(plugin["extension_data"]))
    plugin["reported_uri"] = first_nonempty
    return plugin


def warn_about_plugin_metadata(
    plugin: dict[str, Any], warnings: list[dict[str, Any]]
) -> None:
    """Report questionable LV2 metadata without rewriting the descriptor."""
    for port in plugin["ports"]:
        if port["kind"] != "control" or port["direction"] != "input":
            continue
        minimum = port["minimum"]
        maximum = port["maximum"]
        default = port["default"]
        if None in (minimum, maximum, default):
            continue
        if default < minimum or default > maximum:
            warning(
                warnings,
                "LV2_DEFAULT_OUT_OF_RANGE",
                "La valeur par défaut LV2 d'un port est hors de sa plage déclarée.",
                context={
                    "uri": plugin["uri"],
                    "port_index": port["index"],
                    "symbol": port["symbol"],
                    "minimum": minimum,
                    "maximum": maximum,
                    "default": default,
                },
            )


def plugin_descriptor(plugin: dict[str, Any]) -> dict[str, Any]:
    return {
        key: plugin[key]
        for key in (
            "uri",
            "name",
            "class",
            "author",
            "has_latency",
            "required_features",
            "optional_features",
            "extension_data",
            "ports",
        )
    }


def collect_lv2(
    warnings: list[dict[str, Any]], *, include_raw: bool = False, skip: bool = False
) -> dict[str, Any]:
    if skip:
        return {"status": "skipped", "tools": {}, "plugin_count": 0, "plugins": []}

    lv2ls_path = shutil.which("lv2ls")
    lv2info_path = shutil.which("lv2info")
    tools = {
        "lv2ls": {"path": lv2ls_path, "version": None},
        "lv2info": {"path": lv2info_path, "version": None},
    }
    if not lv2ls_path or not lv2info_path:
        warning(
            warnings,
            "LV2_TOOLS_MISSING",
            "lv2ls et/ou lv2info est absent ; l'inventaire LV2 est incomplet.",
            severity="error",
            context={"lv2ls_found": bool(lv2ls_path), "lv2info_found": bool(lv2info_path)},
        )
        return {"status": "unavailable", "tools": tools, "plugin_count": 0, "plugins": []}

    for name, path in (("lv2ls", lv2ls_path), ("lv2info", lv2info_path)):
        version_result = run_command([path, "--version"], 5.0)
        if version_result["status"] == "ok":
            tools[name]["version"] = version_result["stdout"].strip().splitlines()[0]

    listed = run_command([lv2ls_path], 30.0)
    if listed["status"] != "ok":
        warning(
            warnings,
            "LV2LS_FAILED",
            "lv2ls n'a pas pu énumérer les plugins.",
            severity="error",
            context={"status": listed["status"], "returncode": listed["returncode"]},
        )
        return {"status": "failed", "tools": tools, "plugin_count": 0, "plugins": []}

    uris = sorted({line.strip() for line in listed["stdout"].splitlines() if line.strip()})
    plugins: list[dict[str, Any]] = []
    for uri in uris:
        inspected = run_command([lv2info_path, uri], 20.0)
        if inspected["status"] != "ok":
            warning(
                warnings,
                "LV2INFO_FAILED",
                "lv2info n'a pas pu lire un plugin.",
                severity="error",
                context={"uri": uri, "status": inspected["status"], "returncode": inspected["returncode"]},
            )
            continue

        if inspected.get("stdout_truncated"):
            warning(
                warnings,
                "LV2INFO_TRUNCATED",
                "La sortie lv2info dépasse la limite de sécurité et le plugin a été ignoré.",
                severity="error",
                context={"uri": uri, "limit_bytes": MAX_COMMAND_OUTPUT_BYTES},
            )
            continue

        parsed = parse_lv2info(inspected["stdout"], uri)
        if parsed["reported_uri"] != uri:
            warning(
                warnings,
                "LV2_URI_MISMATCH",
                "L'URI retournée par lv2info ne correspond pas à lv2ls.",
                severity="error",
                context={"requested_uri": uri, "reported_uri": parsed["reported_uri"]},
            )
            continue

        warn_about_plugin_metadata(parsed, warnings)

        descriptor = plugin_descriptor(parsed)
        parsed["plugin_id"] = stable_id("plg", uri)
        parsed["descriptor_scope"] = "lv2info-v1"
        parsed["descriptor_sha256"] = sha256_json(descriptor)
        parsed["source_sha256"] = hashlib.sha256(inspected["stdout"].encode("utf-8")).hexdigest()
        if include_raw:
            parsed["raw_lv2info"] = inspected["stdout"]
        plugins.append(parsed)

    plugins.sort(key=lambda item: item["uri"])
    status_value = "complete" if len(plugins) == len(uris) else "partial"
    return {"status": status_value, "tools": tools, "plugin_count": len(plugins), "plugins": plugins}


def hash_file(path: Path, throttle_mib_per_sec: float) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    started = time.monotonic()
    rate = throttle_mib_per_sec * 1024 * 1024
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            bytes_read += len(block)
            if rate > 0:
                expected_elapsed = bytes_read / rate
                delay = expected_elapsed - (time.monotonic() - started)
                if delay > 0:
                    time.sleep(delay)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def scan_assets(
    data_root: Path,
    warnings: list[dict[str, Any]],
    *,
    throttle_mib_per_sec: float = 8.0,
    trusted_symlink_roots: Mapping[str, tuple[Path, ...]] | None = None,
) -> dict[str, Any]:
    upload_root = data_root / "audio_uploads"
    roots: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    try:
        upload_root_resolved = upload_root.resolve(strict=True)
    except OSError:
        warning(
            warnings,
            "UPLOAD_ROOT_MISSING",
            "La racine audio_uploads n'existe pas ou n'est pas accessible.",
            severity="error",
            context={"path": str(upload_root)},
        )
        return {"upload_root": str(upload_root), "roots": [], "asset_count": 0, "items": []}

    trusted_roots_by_kind = (
        DEFAULT_TRUSTED_SYMLINK_ROOTS
        if trusted_symlink_roots is None
        else trusted_symlink_roots
    )

    for relative_directory, kind, allowed_extensions in ASSET_DIRECTORIES:
        category_root = upload_root / relative_directory
        root_record = {
            "kind": kind,
            "relative_directory": relative_directory,
            "exists": category_root.is_dir(),
        }
        roots.append(root_record)
        if not category_root.is_dir():
            continue
        if category_root.is_symlink():
            warning(
                warnings,
                "SYMLINK_SKIPPED",
                "Une racine de médias symbolique a été ignorée.",
                context={"relative_path": relative_directory},
            )
            continue

        try:
            category_resolved = category_root.resolve(strict=True)
        except OSError as exc:
            warning(
                warnings,
                "ASSET_ROOT_UNREADABLE",
                "Une racine de médias ne peut pas être résolue.",
                severity="error",
                context={"relative_directory": relative_directory, "error": str(exc)},
            )
            continue
        if not is_within(category_resolved, upload_root_resolved):
            warning(
                warnings,
                "ASSET_ROOT_OUTSIDE_UPLOADS",
                "Une racine de médias sort de audio_uploads et a été ignorée.",
                severity="error",
                context={"relative_directory": relative_directory},
            )
            continue


        trusted_roots: list[Path] = []
        for trusted_root in trusted_roots_by_kind.get(kind, ()):
            try:
                resolved_trusted_root = trusted_root.resolve(strict=True)
                if resolved_trusted_root.is_dir():
                    trusted_roots.append(resolved_trusted_root)
            except OSError:
                # A package root that is not installed is simply unavailable.
                # A warning is only useful if an actual link tries to use it.
                continue

        for current_root, directory_names, file_names in os.walk(
            category_resolved, topdown=True, followlinks=False
        ):
            current_path = Path(current_root)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                child = current_path / name
                if child.is_symlink():
                    warning(
                        warnings,
                        "SYMLINK_SKIPPED",
                        "Un lien symbolique de répertoire a été ignoré.",
                        context={"relative_path": child.relative_to(upload_root_resolved).as_posix()},
                    )
                else:
                    safe_directories.append(name)
            directory_names[:] = safe_directories

            for name in sorted(file_names):
                path = current_path / name
                relative_path = path.relative_to(upload_root_resolved).as_posix()
                if path.suffix.lower() not in allowed_extensions:
                    continue

                try:
                    is_symlink = path.is_symlink()
                    resolved = path.resolve(strict=True)
                    target_allowed = is_within(resolved, category_resolved) or any(
                        is_within(resolved, trusted_root) for trusted_root in trusted_roots
                    )
                    if not target_allowed:
                        if is_symlink:
                            warning(
                                warnings,
                                "SYMLINK_TARGET_NOT_ALLOWED",
                                "La cible d'un média symbolique est hors des racines autorisées.",
                                context={"relative_path": relative_path},
                            )
                            continue
                        raise ValueError("fichier hors de la racine autorisée")
                    before = resolved.stat()
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    sha256 = hash_file(resolved, throttle_mib_per_sec)
                    after = resolved.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        raise RuntimeError("fichier modifié pendant le calcul du hash")
                except (OSError, ValueError, RuntimeError) as exc:
                    warning(
                        warnings,
                        "ASSET_READ_FAILED",
                        "Un média n'a pas été ajouté au catalogue.",
                        severity="error",
                        context={"relative_path": relative_path, "error": str(exc)},
                    )
                    continue

                items.append(
                    {
                        "asset_id": stable_id("ast", kind, relative_path, sha256),
                        "kind": kind,
                        "relative_path": relative_path,
                        "extension": path.suffix.lower(),
                        "size_bytes": before.st_size,
                        "mtime_ns": before.st_mtime_ns,
                        "sha256": sha256,
                    }
                )

    items.sort(key=lambda item: (item["kind"], item["relative_path"]))
    return {
        "upload_root": str(upload_root),
        "roots": roots,
        "asset_count": len(items),
        "items": items,
    }


def build_catalog_projection(lv2: dict[str, Any], assets: dict[str, Any]) -> dict[str, Any]:
    plugins = []
    for plugin in lv2["plugins"]:
        descriptor = plugin_descriptor(plugin)
        descriptor["plugin_id"] = plugin["plugin_id"]
        descriptor["descriptor_scope"] = plugin["descriptor_scope"]
        descriptor["descriptor_sha256"] = plugin["descriptor_sha256"]
        plugins.append(descriptor)

    asset_projection = [
        {
            "asset_id": item["asset_id"],
            "kind": item["kind"],
            "relative_path": item["relative_path"],
            "extension": item["extension"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in assets["items"]
    ]
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "plugins": plugins,
        "assets": asset_projection,
    }


def lower_process_priority(warnings: list[dict[str, Any]]) -> None:
    if os.name != "posix":
        return
    try:
        os.nice(10)
    except OSError as exc:
        warning(
            warnings,
            "NICE_FAILED",
            "La priorité CPU du collecteur n'a pas pu être réduite.",
            context={"error": str(exc)},
        )


def collect_inventory(
    data_root: Path,
    *,
    throttle_mib_per_sec: float = 8.0,
    include_raw_lv2info: bool = False,
    skip_lv2: bool = False,
    reduce_priority: bool = True,
) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    if reduce_priority:
        lower_process_priority(warnings)

    host = collect_host()
    pipedal = collect_pipedal(data_root, warnings)
    lv2 = collect_lv2(warnings, include_raw=include_raw_lv2info, skip=skip_lv2)
    assets = scan_assets(data_root, warnings, throttle_mib_per_sec=throttle_mib_per_sec)
    projection = build_catalog_projection(lv2, assets)
    errors = sum(1 for item in warnings if item["severity"] == "error")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "status": "complete" if errors == 0 else "partial",
        "collector": {
            "name": "pipedal-ai-inspect",
            "version": COLLECTOR_VERSION,
            "network_access": False,
            "shell_execution": False,
        },
        "host": host,
        "pipedal": pipedal,
        "lv2": lv2,
        "assets": assets,
        "catalog": {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "version": None,
            "sha256": sha256_json(projection),
            "plugin_count": len(projection["plugins"]),
            "asset_count": len(projection["assets"]),
        },
        "warnings": warnings,
    }


def atomic_write_json(path: Path, value: Any, *, force: bool) -> None:
    output = path.expanduser().absolute()
    if not output.parent.is_dir():
        raise FileNotFoundError(f"Le dossier de sortie n'existe pas : {output.parent}")
    if output.exists() and not force:
        raise FileExistsError(f"Le fichier existe déjà : {output}. Utiliser --force pour le remplacer.")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventaire PiPedal/LV2/NAM/IR en lecture seule pour PiPedal AI."
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help="Racine de données PiPedal (défaut : /var/pipedal).",
    )
    parser.add_argument("--output", type=Path, required=True, help="Fichier JSON à produire.")
    parser.add_argument(
        "--throttle-mib-per-sec",
        type=float,
        default=8.0,
        help="Débit maximal approximatif pour le hash des médias ; 0 désactive la limite.",
    )
    parser.add_argument(
        "--include-raw-lv2info",
        action="store_true",
        help="Inclure la sortie lv2info brute pour diagnostic ; elle peut contenir des chemins locaux.",
    )
    parser.add_argument(
        "--skip-lv2",
        action="store_true",
        help="Ignorer temporairement l'inventaire LV2 ; destiné aux tests, pas au catalogue final.",
    )
    parser.add_argument(
        "--no-low-priority",
        action="store_true",
        help="Ne pas réduire la priorité CPU du collecteur.",
    )
    parser.add_argument("--force", action="store_true", help="Remplacer le fichier de sortie.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.throttle_mib_per_sec < 0:
        parser.error("--throttle-mib-per-sec doit être positif ou nul")

    try:
        inventory = collect_inventory(
            args.data_root,
            throttle_mib_per_sec=args.throttle_mib_per_sec,
            include_raw_lv2info=args.include_raw_lv2info,
            skip_lv2=args.skip_lv2,
            reduce_priority=not args.no_low_priority,
        )
        atomic_write_json(args.output, inventory, force=args.force)
    except Exception as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1

    print(f"Inventaire écrit : {args.output.absolute()}")
    print(f"Statut : {inventory['status']}")
    print(f"Plugins LV2 : {inventory['catalog']['plugin_count']}")
    print(f"NAM/IR : {inventory['catalog']['asset_count']}")
    print(f"SHA-256 du catalogue : {inventory['catalog']['sha256']}")
    if inventory["warnings"]:
        print(f"Avertissements : {len(inventory['warnings'])}")
    return 0 if inventory["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
