"""Bounded local audio/file operations shared by Pi and compute services."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

import numpy as np
import soundfile as sf


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_path(root: Path, relative: str, *, exists=True) -> Path:
    if root.is_symlink():
        raise ValueError("La racine ne doit pas être un lien symbolique")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(p in (".", "..") for p in rel.parts) or "\\" in relative:
        raise ValueError("Chemin relatif non sûr")
    root = root.absolute()
    path = root
    for component in rel.parts:
        path = path / component
        if path.is_symlink():
            raise ValueError("Lien symbolique refusé")
    path.resolve(strict=exists).relative_to(root.resolve(strict=True))
    if exists and not path.is_file():
        raise ValueError("Fichier ordinaire requis")
    return path


def atomic_json(path: Path, value, *, overwrite=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (path.exists() and not overwrite):
        raise ValueError("Fichier existant ou lien refusé")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pipedal-ai-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_audio(path: Path, *, max_seconds=180.0, require_di=False):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Audio absent, lié ou trop grand")
    try:
        info = sf.info(path)
    except (RuntimeError, OSError) as exc:
        raise ValueError("Fichier audio non lisible") from exc
    if info.frames < 1 or info.duration > max_seconds or info.channels not in (1, 2) or info.samplerate != 48000:
        raise ValueError("Audio attendu : 48 kHz, mono/stéréo, 0..180 secondes")
    if info.format not in ("WAV", "WAVEX") or info.subtype not in ("PCM_16", "PCM_24", "PCM_32", "FLOAT"):
        raise ValueError("WAV PCM ou float requis")
    if require_di and (info.channels != 1 or info.subtype != "PCM_24"):
        raise ValueError("DI attendue : WAV mono PCM 24 bits à 48 kHz")
    try:
        data, rate = sf.read(path, dtype="float32", always_2d=True)
    except (RuntimeError, OSError) as exc:
        raise ValueError("Décodage audio échoué") from exc
    if not np.isfinite(data).all():
        raise ValueError("Audio non fini")
    return data, rate


def write_pcm(path: Path, data, rate=48000):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        raise ValueError("Audio existant refusé")
    sf.write(path, data, rate, subtype="PCM_24", format="WAV")
    os.chmod(path, 0o600)


def audio_levels(data):
    return {"peak_dbfs": float(20 * np.log10(max(1e-12, float(np.max(np.abs(data)))))),
            "rms_dbfs": float(20 * np.log10(max(1e-12, float(np.sqrt(np.mean(data.astype('float64') ** 2)))))),
            "clipped_ratio": float(np.mean(np.abs(data) >= 0.999)),
            "silence_ratio": float(np.mean(np.abs(data) < 1e-5))}


def preview_audio(path: Path, output: Path, *, target_rms=-24.0, ceiling=-3.0):
    data, rate = read_audio(path)
    levels = audio_levels(data)
    change = min(target_rms - levels["rms_dbfs"], ceiling - levels["peak_dbfs"], 12.0)
    write_pcm(output, data * 10 ** (change / 20), rate)
    return {"gain_db": change, "method": "RMS matching with peak ceiling; not LUFS",
            "levels": audio_levels(data * 10 ** (change / 20))}
