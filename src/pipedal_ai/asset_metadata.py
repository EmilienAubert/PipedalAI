"""Pi-owned extraction of content-hashed local NAM and IR metadata."""
from __future__ import annotations

import json
import math
import soundfile as sf
from datetime import datetime, timezone
from io import BytesIO

from .compiler import PresetCompiler


METADATA_VERSION = "pipedal-ai.local-asset-metadata/1.0.0"


def capture_info(asset: dict) -> dict:
    metadata = asset.get("metadata") or {}
    local = metadata.get("nam_file", {})
    declared = []
    raw = local.get("gear_type")
    normalized = {"amp": "amp", "amp-only": "amp", "amp_only": "amp",
                  "full-rig": "amp-cab", "full_rig": "amp-cab", "amp-cab": "amp-cab"}
    if isinstance(raw, str) and raw in normalized:
        declared.append(("nam_file", normalized[raw]))
    tone = metadata.get("tone3000", {})
    if isinstance(tone.get("gear"), str) and tone["gear"] in normalized:
        declared.append(("tone3000", normalized[tone["gear"]]))
    direct = asset.get("capture_type")
    if isinstance(direct, str) and direct.casefold().replace("_", "-") in normalized:
        declared.append(("catalogue", normalized[direct.casefold().replace("_", "-")]))
    types = {value for _, value in declared}
    if len(types) > 1:
        return {"capture_type": "unknown", "capture_provenance": "conflict",
                "capture_confidence": "conflicting-declarations"}
    return {"capture_type": next(iter(types), "unknown"),
            "capture_provenance": declared[0][0] if declared else "unknown",
            "capture_confidence": "declared" if declared else "unknown"}


def enrich_local_assets(catalog, compiler: PresetCompiler) -> dict:
    active = catalog.active_ref()
    with catalog.database.connect() as connection:
        rows = list(connection.execute("SELECT * FROM catalog_assets WHERE revision=?", (active.revision,)))
        cached = {(r["asset_id"], r["source"]): json.loads(r["metadata_json"]) for r in
                  connection.execute("SELECT * FROM asset_metadata WHERE revision=?", (active.revision,))}
    result = {"catalog": active.model_dump(mode="json"), "extracted": 0, "cached": 0, "warnings": []}
    timestamp = datetime.now(timezone.utc).isoformat()
    for row in rows:
        source = "nam_file" if row["kind"] == "nam" else "ir_file"
        if cached.get((row["asset_id"], source), {}).get("asset_sha256") == row["sha256"]:
            result["cached"] += 1
            continue
        try:
            if row["size_bytes"] > 128 * 1024 * 1024:
                raise ValueError("Fichier trop grand pour l'extraction locale bornée")
            content = compiler._verified_asset(row["relative_path"], row["size_bytes"], row["sha256"])
            info = {"schema_version": METADATA_VERSION, "asset_sha256": row["sha256"]}
            if source == "nam_file":
                raw = json.loads(content)
                if not isinstance(raw, dict) or not isinstance(raw.get("metadata", {}), dict):
                    raise ValueError("Structure NAM non prise en charge")
                meta = raw.get("metadata", {})
                for key in ("name", "modeled_by", "gear_make", "gear_model", "gear_type", "tone_type"):
                    if isinstance(meta.get(key), str):
                        info[key] = meta[key][:300]
                for key in ("input_level_dbu", "output_level_dbu", "loudness", "gain"):
                    value = meta.get(key)
                    if type(value) in (int, float) and -200 <= value <= 200:
                        info[key] = value
                if type(raw.get("sample_rate")) in (int, float) and math.isfinite(raw["sample_rate"]) and 8000 <= raw["sample_rate"] <= 384000:
                    info["sample_rate_hz"] = raw["sample_rate"]
                if isinstance(raw.get("architecture"), str):
                    info["architecture"] = raw["architecture"][:80]
            else:
                audio = sf.info(BytesIO(content))
                info.update(sample_rate_hz=audio.samplerate, channels=audio.channels,
                            duration_seconds=audio.duration, subtype=audio.subtype)
            # No write if catalogue changed while files were being read.
            if catalog.active_ref() != active:
                raise ValueError("Catalogue changé pendant l'enrichissement")
            with catalog.database.transaction() as connection:
                connection.execute("""INSERT INTO asset_metadata VALUES(?,?,?,?,?)
                    ON CONFLICT(revision,asset_id,source) DO UPDATE SET
                    metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                    (active.revision, row["asset_id"], source, catalog.database.json(info), timestamp))
            result["extracted"] += 1
        except Exception as exc:
            result["warnings"].append({"asset_id": row["asset_id"], "error": str(exc)[:200]})
    return result
