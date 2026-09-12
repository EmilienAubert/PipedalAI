from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from .audio_io import atomic_json, audio_levels, digest_file, read_audio, safe_path
from .models import StrictModel


class DIFile(StrictModel):
    di_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    file: str = Field(max_length=200)
    purpose: str = Field(max_length=80)
    guitar: str = Field(max_length=120)
    pickup_type: str = Field(max_length=40)
    pickup_position: str = Field(max_length=40)
    guitar_volume: float | int = Field(ge=0, le=10)
    guitar_tone: float | int = Field(ge=0, le=10)
    input_gain_note: str = Field(max_length=200)
    performance: str = Field(max_length=240)
    notes: str = Field(default="", max_length=500)


class DIManifest(StrictModel):
    schema_version: str = Field(pattern=r"^pipedal-ai\.di-manifest/1\.0\.0$")
    set_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    sample_rate_hz: int = Field(ge=48000, le=48000)
    pcm_bits: int = Field(ge=24, le=24)
    channels: int = Field(ge=1, le=1)
    files: list[DIFile] = Field(min_length=1, max_length=32)


def ingest_di(database, manifest_path: Path, storage: Path):
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 128000:
        raise ValueError("Manifeste DI invalide")
    manifest = DIManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    ids = [f.di_id for f in manifest.files]
    if len(ids) != len(set(ids)) or len({f.file for f in manifest.files}) != len(ids):
        raise ValueError("Identifiants et chemins DI doivent être uniques")
    records = []
    for item in manifest.files:
        path = safe_path(manifest_path.parent, item.file)
        data, rate = read_audio(path, require_di=True)
        levels = audio_levels(data)
        if levels["peak_dbfs"] > -1 or levels["rms_dbfs"] < -65:
            raise ValueError(f"DI {item.di_id} écrêtée/proche de l'écrêtage ou silencieuse")
        records.append({"di_id": item.di_id, "source": path, "sha256": digest_file(path),
                        "duration": len(data) / rate, "levels": levels})
    payload = {"manifest": manifest.model_dump(mode="json"),
               "audio": [{k: v for k, v in r.items() if k != "source"} for r in records]}
    import hashlib
    digest = hashlib.sha256(database.json(payload).encode()).hexdigest()
    with database.connect() as c:
        old = c.execute("SELECT * FROM di_sets WHERE set_id=?", (manifest.set_id,)).fetchone()
    if old:
        if old["sha256"] != digest:
            raise ValueError("Ce set_id existe avec un autre contenu : choisir un nouvel identifiant")
        return dict(old)
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = safe_path(storage, digest, exists=False)
    if destination.exists():
        raise ValueError("Dossier DI existant sans inscription SQLite")
    destination.mkdir(mode=0o700)
    try:
        for record in records:
            target = destination / (record["di_id"] + ".wav")
            shutil.copyfile(record["source"], target)
            target.chmod(0o600)
            if digest_file(target) != record["sha256"]:
                raise ValueError("DI modifiée pendant la copie")
        atomic_json(destination / "manifest.json", payload)
        with database.transaction() as c:
            c.execute("INSERT INTO di_sets VALUES(?,?,?,?,?)", (manifest.set_id, digest,
                      str(destination.absolute()), database.json(payload), datetime.now(timezone.utc).isoformat()))
        return {"set_id": manifest.set_id, "sha256": digest, "root": str(destination.absolute()), "file_count": len(records)}
    except BaseException:
        shutil.rmtree(destination)
        raise
