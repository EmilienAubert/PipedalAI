"""Paired DI/render measurements. Perceptual descriptors are explicitly proxies."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from scipy.signal import correlate, correlation_lags, welch

from ..audio_io import audio_levels, digest_file, read_audio


ANALYSIS_VERSION = "pipedal-ai.paired-analysis/1.0.0"


def blocks(data, size=960):
    # Power aggregation avoids cancelling opposite-phase stereo channels.
    mono = np.sqrt(np.mean(data.astype("float64") ** 2, axis=1))
    count = len(mono) // size
    if count < 4:
        raise ValueError("Audio trop court pour la mesure appariée")
    return np.sqrt(np.mean(mono[:count * size].reshape(count, size) ** 2, axis=1))


def analyze_pair(di_path: Path, render_path: Path):
    di, rate = read_audio(di_path)
    rendered, _ = read_audio(render_path)
    a, b = blocks(di), blocks(rendered)
    # Align amplitude envelopes, not raw waveforms distorted by a cabinet/amp.
    x, y = a - np.mean(a), b - np.mean(b)
    c = correlate(y, x, method="fft")
    lags = correlation_lags(len(y), len(x))
    valid = np.abs(lags) <= 100  # <= 2 seconds; reject unbounded timing drift.
    lag = int(lags[valid][np.argmax(c[valid])]) if np.linalg.norm(x)*np.linalg.norm(y)>1e-12 else 0
    if lag >= 0:
        aligned_a, aligned_b = a[:len(b) - lag], b[lag:]
    else:
        aligned_a, aligned_b = a[-lag:], b[:len(a) + lag]
    count = min(len(aligned_a), len(aligned_b))
    aligned_a, aligned_b = aligned_a[:count], aligned_b[:count]
    mask = (aligned_a > 1e-4) & (aligned_b > 1e-6)
    if int(mask.sum()) < 8:
        raise ValueError("Rendu silencieux ou trop peu de signal apparié")
    in_db = 20 * np.log10(aligned_a[mask])
    out_db = 20 * np.log10(aligned_b[mask])
    span = float(np.percentile(in_db, 90) - np.percentile(in_db, 10))
    slope = float(np.polyfit(in_db, out_db, 1)[0]) if span > 3 else None
    correlation = float(np.corrcoef(in_db, out_db)[0, 1]) if np.std(in_db) > 0.1 and np.std(out_db) > 0.1 else 0.0
    start = max(0, lag * 960)
    segment = rendered[start:start + min(len(di), len(rendered) - start)]
    frequencies, channel_energy = welch(segment, fs=rate, nperseg=min(4096, len(segment)), axis=0)
    energy = np.mean(channel_energy, axis=1)
    total = max(1e-15, float(energy.sum()))
    bands = [(20, 250), (250, 1000), (1000, 4000), (4000, 10000), (10000, 24000)]
    ratios = [float(energy[(frequencies >= low) & (frequencies < high)].sum() / total) for low, high in bands]
    levels = audio_levels(rendered)
    di_levels = audio_levels(di)
    compression = float(np.clip(1 - slope, 0, 1)) if slope is not None else None
    # Loudness-independent brightness/warmth; dynamic slope is a proxy, not a
    # ground-truth gain or distortion amount for arbitrary musical material.
    features = {"bass": ratios[0], "mids": ratios[1] + ratios[2], "treble": ratios[3] + ratios[4],
                "warmth": float(np.clip(0.5 + ratios[1] - ratios[3], 0, 1)),
                "brightness": float(np.clip((ratios[3] + ratios[4]) * 2.5, 0, 1)),
                "compression_proxy": compression, "pick_sensitivity_proxy":
                float(np.clip(slope, 0, 1)) if slope is not None else None,
                "fizz_proxy": ratios[4]}
    warnings = []
    if span <= 3:
        warnings.append("DI peu dynamique : compression/sensibilité non mesurables")
    if correlation < 0.3:
        warnings.append("Correspondance des enveloppes faible : interpréter la dynamique avec prudence")
    if levels["clipped_ratio"] > 0:
        warnings.append("Échantillons proches du plein niveau : vérifier écrêtage ou saturation intentionnelle")
    return {"schema_version": ANALYSIS_VERSION, "di_sha256": digest_file(di_path),
            "render_sha256": digest_file(render_path), "sample_rate_hz": rate,
            "duration_seconds": len(rendered) / rate, "alignment_seconds": lag * 0.02,
            "alignment_confidence": correlation, "input_dynamic_span_db": span,
            "envelope_slope": slope, "di_levels": di_levels, "render_levels": levels,
            "features": features, "warnings": warnings,
            "limitations": "Spectral/envelope proxies; no learned embedding or exact distortion estimate"}


def score_features(features, intent):
    spectrum = intent.spectrum if hasattr(intent, "spectrum") else None
    if spectrum is None:
        from ..intent import ToneIntent
        intent = ToneIntent.model_validate(intent)
        spectrum = intent.spectrum
    targets = {"warmth": spectrum.warmth, "brightness": spectrum.brightness,
               "compression_proxy": intent.dynamics.compression,
               "pick_sensitivity_proxy": intent.dynamics.pick_sensitivity}
    targets["saturation_proxy"] = intent.gain.amount
    weights = {"warmth": 1.0, "brightness": 1.0, "compression_proxy": 0.7,
               "pick_sensitivity_proxy": 0.7}
    weights["saturation_proxy"] = 0.5
    for value in features.values():
        if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError("Mesure non finie ou non numérique")
    components = {k: weights[k] * (float(features[k]) - target) ** 2 for k, target in targets.items()
                  if features.get(k) is not None}
    return {"total": float(sum(components.values())), "components": components,
            "method": "heuristic-text-target-distance/1.0.0"}


def multilevel_summary(measurements):
    if len(measurements) < 2:
        raise ValueError("Au moins deux niveaux DI sûrs sont nécessaires")
    keys = measurements[0]["analysis"]["features"]
    features = {key: float(np.mean([m["analysis"]["features"][key] for m in measurements
                                   if m["analysis"]["features"][key] is not None]))
                if any(m["analysis"]["features"][key] is not None for m in measurements) else None for key in keys}
    levels = sorted(measurements, key=lambda m: m["input_gain_db"])
    gains = [float(m["input_gain_db"]) for m in levels]
    out = [m["analysis"]["render_levels"]["rms_dbfs"] for m in levels]
    slope = float(np.polyfit(gains, out, 1)[0]) if len(set(gains)) > 1 else None
    features["saturation_proxy"] = float(np.clip(1-slope, 0, 1)) if slope is not None else None
    return {"schema_version": "pipedal-ai.characterization/1.0.0", "analysis_version": ANALYSIS_VERSION,
            "features": features, "level_response_slope": slope,
            "measurements": levels, "confidence": "measured-proxies",
            "context": {"di_sha256": levels[0]["analysis"]["di_sha256"], "sample_rate_hz": 48000},
            "limitations": "Valid for this DI, calibration, host and associated IR; not an isolated universal NAM signature"}
