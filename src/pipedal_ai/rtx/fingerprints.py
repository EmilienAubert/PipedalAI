"""Deterministic audio fingerprints and a small persistent local index.

The extractor intentionally uses only the Python standard library.  It is not a
replacement for a perceptual model, but gives the text-mode ranker stable,
measurable features while leaving room for a learned embedding later.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import ConfigDict, Field, StrictFloat, StrictInt
from pydantic import BaseModel


FINGERPRINT_SCHEMA_VERSION = "pipedal-ai.audio-fingerprint/1.0.0"
INDEX_SCHEMA_VERSION = "pipedal-ai.fingerprint-index/1.0.0"
EXTRACTOR_VERSION = "stdlib-pcm/1.0.0"
_DB_FLOOR = -120.0


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SpectralFeatures(_StrictModel):
    """Energy ratios; all bands together sum to one for non-silent audio."""

    sub: StrictFloat = Field(ge=0.0, le=1.0)
    bass: StrictFloat = Field(ge=0.0, le=1.0)
    low_mid: StrictFloat = Field(ge=0.0, le=1.0)
    mid: StrictFloat = Field(ge=0.0, le=1.0)
    high_mid: StrictFloat = Field(ge=0.0, le=1.0)
    treble: StrictFloat = Field(ge=0.0, le=1.0)
    air: StrictFloat = Field(ge=0.0, le=1.0)
    centroid_hz: StrictFloat = Field(ge=0.0)
    brightness: StrictFloat = Field(ge=0.0, le=1.0)
    high_frequency_fizz: StrictFloat = Field(ge=0.0, le=1.0)


class DynamicFeatures(_StrictModel):
    crest_factor_db: StrictFloat = Field(ge=0.0, le=120.0)
    block_dynamic_range_db: StrictFloat = Field(ge=0.0, le=120.0)
    transient_density_hz: StrictFloat = Field(ge=0.0)
    pick_sensitivity: StrictFloat = Field(ge=0.0, le=1.0)
    compression: StrictFloat = Field(ge=0.0, le=1.0)


class ToneFeatures(_StrictModel):
    """Normalized descriptors used by the text-mode nearest-neighbour ranker."""

    gain: StrictFloat = Field(ge=0.0, le=1.0)
    bass: StrictFloat = Field(ge=0.0, le=1.0)
    mids: StrictFloat = Field(ge=0.0, le=1.0)
    treble: StrictFloat = Field(ge=0.0, le=1.0)
    warmth: StrictFloat = Field(ge=0.0, le=1.0)
    bass_tightness: StrictFloat = Field(ge=0.0, le=1.0)
    noise: StrictFloat = Field(ge=0.0, le=1.0)


class AudioFingerprint(_StrictModel):
    schema_version: Literal["pipedal-ai.audio-fingerprint/1.0.0"]
    extractor_version: Literal["stdlib-pcm/1.0.0"]
    asset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_kind: Literal["nam", "cab_ir", "reverb_ir", "render", "reference", "unknown"]
    sample_rate_hz: StrictInt = Field(ge=1, le=768000)
    channels: StrictInt = Field(ge=1, le=2)
    sample_width_bytes: StrictInt = Field(ge=1, le=4)
    frame_count: StrictInt = Field(ge=1)
    duration_seconds: StrictFloat = Field(gt=0.0)
    rms_dbfs: StrictFloat = Field(ge=-120.0, le=0.0)
    peak_dbfs: StrictFloat = Field(ge=-120.0, le=0.0)
    clipped_sample_ratio: StrictFloat = Field(ge=0.0, le=1.0)
    silence_ratio: StrictFloat = Field(ge=0.0, le=1.0)
    zero_crossing_rate: StrictFloat = Field(ge=0.0, le=1.0)
    spectral: SpectralFeatures
    dynamics: DynamicFeatures
    tone: ToneFeatures


class FingerprintDistance(_StrictModel):
    total: StrictFloat = Field(ge=0.0)
    compared_features: StrictInt = Field(ge=0)
    components: dict[str, StrictFloat]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _db(value: float) -> float:
    if value <= 1e-12:
        return _DB_FLOOR
    return max(_DB_FLOOR, min(0.0, 20.0 * math.log10(value)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return _DB_FLOOR
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    blend = position - lower
    return ordered[lower] * (1.0 - blend) + ordered[upper] * blend


def _decode_pcm(raw: bytes, sample_width: int) -> list[float]:
    """Decode little-endian unsigned 8-bit or signed 16/24/32-bit PCM."""
    if sample_width == 1:
        return [(value - 128) / 128.0 for value in raw]
    if sample_width == 2:
        count = len(raw) // 2
        return [value / 32768.0 for value in struct.unpack(f"<{count}h", raw)]
    if sample_width == 3:
        result = []
        for offset in range(0, len(raw), 3):
            value = int.from_bytes(raw[offset:offset + 3], "little", signed=False)
            if value & 0x800000:
                value -= 1 << 24
            result.append(value / 8388608.0)
        return result
    if sample_width == 4:
        count = len(raw) // 4
        return [value / 2147483648.0 for value in struct.unpack(f"<{count}i", raw)]
    raise ValueError(f"Unsupported PCM sample width: {sample_width} bytes")


def _mono(samples: list[float], channels: int) -> list[float]:
    if channels == 1:
        return samples
    return [(samples[index] + samples[index + 1]) * 0.5 for index in range(0, len(samples), 2)]


def _fft(values: list[complex]) -> None:
    """In-place radix-2 FFT, sufficient for deterministic coarse band energy."""
    size = len(values)
    index = 1
    reverse = 0
    while index < size:
        bit = size >> 1
        while reverse & bit:
            reverse ^= bit
            bit >>= 1
        reverse ^= bit
        if index < reverse:
            values[index], values[reverse] = values[reverse], values[index]
        index += 1
    length = 2
    while length <= size:
        root = complex(math.cos(-2.0 * math.pi / length), math.sin(-2.0 * math.pi / length))
        for start in range(0, size, length):
            factor = 1.0 + 0.0j
            half = length // 2
            for offset in range(half):
                even = values[start + offset]
                odd = factor * values[start + offset + half]
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                factor *= root
        length <<= 1


def _band_name(frequency: float) -> str | None:
    if frequency < 20.0:
        return None
    if frequency < 80.0:
        return "sub"
    if frequency < 250.0:
        return "bass"
    if frequency < 500.0:
        return "low_mid"
    if frequency < 2000.0:
        return "mid"
    if frequency < 5000.0:
        return "high_mid"
    if frequency < 12000.0:
        return "treble"
    return "air"


def _spectral_features(path: Path, frame_count: int, sample_rate: int, channels: int, sample_width: int) -> SpectralFeatures:
    fft_size = 4096
    if frame_count < fft_size:
        fft_size = 1 << max(5, (frame_count.bit_length() - 1))
    window_count = min(8, max(1, frame_count // fft_size))
    maximum_start = max(0, frame_count - fft_size)
    starts = [round(maximum_start * index / max(1, window_count - 1)) for index in range(window_count)]
    energies = {name: 0.0 for name in ("sub", "bass", "low_mid", "mid", "high_mid", "treble", "air")}
    centroid_numerator = 0.0
    total_energy = 0.0

    with wave.open(str(path), "rb") as source:
        for start in starts:
            source.setpos(start)
            values = _mono(_decode_pcm(source.readframes(fft_size), sample_width), channels)
            if len(values) < fft_size:
                values.extend([0.0] * (fft_size - len(values)))
            transformed = [
                complex(sample * (0.5 - 0.5 * math.cos(2.0 * math.pi * index / max(1, fft_size - 1))), 0.0)
                for index, sample in enumerate(values)
            ]
            _fft(transformed)
            for index in range(1, fft_size // 2 + 1):
                frequency = index * sample_rate / fft_size
                band = _band_name(frequency)
                if band is None:
                    continue
                energy = transformed[index].real ** 2 + transformed[index].imag ** 2
                energies[band] += energy
                centroid_numerator += frequency * energy
                total_energy += energy

    if total_energy <= 1e-18:
        ratios = {name: 0.0 for name in energies}
        centroid = 0.0
    else:
        ratios = {name: value / total_energy for name, value in energies.items()}
        centroid = centroid_numerator / total_energy
    brightness = _clamp(ratios["high_mid"] + ratios["treble"] + ratios["air"])
    fizz = _clamp(ratios["treble"] + ratios["air"])
    return SpectralFeatures(
        **ratios,
        centroid_hz=float(centroid),
        brightness=float(brightness),
        high_frequency_fizz=float(fizz),
    )


def extract_wav_fingerprint(
    wav_path: str | os.PathLike[str],
    *,
    asset_sha256: str | None = None,
    source_kind: Literal["nam", "cab_ir", "reverb_ir", "render", "reference", "unknown"] = "render",
) -> AudioFingerprint:
    """Extract a bounded, deterministic fingerprint from an uncompressed PCM WAV."""
    path = Path(wav_path)
    audio_sha256 = _sha256_file(path)
    asset_digest = asset_sha256 or audio_sha256
    if len(asset_digest) != 64 or any(character not in "0123456789abcdef" for character in asset_digest):
        raise ValueError("asset_sha256 must be a lowercase SHA-256 digest")

    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("Only uncompressed PCM WAV files are supported")
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        frame_count = source.getnframes()
        if channels not in (1, 2):
            raise ValueError("Only mono and stereo WAV files are supported")
        if sample_width not in (1, 2, 3, 4):
            raise ValueError("Only 8, 16, 24 and 32-bit integer PCM WAV files are supported")
        if frame_count < 1 or sample_rate < 1:
            raise ValueError("The WAV file contains no audio frames")

        sum_squares = 0.0
        peak = 0.0
        clipped = 0
        sample_count = 0
        previous_mono: float | None = None
        zero_crossings = 0
        block_db: list[float] = []
        block_size = max(1, round(sample_rate * 0.02))
        block_squares = 0.0
        block_frames = 0
        previous_block_db: float | None = None
        transients = 0

        while True:
            raw = source.readframes(16384)
            if not raw:
                break
            values = _decode_pcm(raw, sample_width)
            sample_count += len(values)
            for value in values:
                absolute = abs(value)
                sum_squares += value * value
                peak = max(peak, absolute)
                if absolute >= 1.0 - (1.0 / (1 << (sample_width * 8 - 1))):
                    clipped += 1
            mono_values = _mono(values, channels)
            for value in mono_values:
                if previous_mono is not None and ((previous_mono < 0.0 <= value) or (previous_mono >= 0.0 > value)):
                    zero_crossings += 1
                previous_mono = value
                block_squares += value * value
                block_frames += 1
                if block_frames >= block_size:
                    level = _db(math.sqrt(block_squares / block_frames))
                    block_db.append(level)
                    if previous_block_db is not None and level - previous_block_db >= 6.0 and level > -50.0:
                        transients += 1
                    previous_block_db = level
                    block_squares = 0.0
                    block_frames = 0
        if block_frames:
            level = _db(math.sqrt(block_squares / block_frames))
            block_db.append(level)
            if previous_block_db is not None and level - previous_block_db >= 6.0 and level > -50.0:
                transients += 1

    duration = frame_count / sample_rate
    rms = math.sqrt(sum_squares / max(1, sample_count))
    rms_dbfs = _db(rms)
    peak_dbfs = _db(peak)
    crest = _clamp(peak_dbfs - rms_dbfs, 0.0, 120.0)
    active_blocks = [level for level in block_db if level > -90.0]
    dynamic_range = max(0.0, _percentile(active_blocks, 0.90) - _percentile(active_blocks, 0.10)) if active_blocks else 0.0
    silence_ratio = sum(level <= -60.0 for level in block_db) / max(1, len(block_db))
    spectral = _spectral_features(path, frame_count, sample_rate, channels, sample_width)

    effectively_silent = rms_dbfs <= -100.0
    compression = 0.0 if effectively_silent else _clamp((18.0 - crest) / 14.0)
    pick_sensitivity = 0.0 if effectively_silent else _clamp(
        (dynamic_range / 30.0) * 0.7 + min(transients / max(duration, 1e-9), 8.0) / 8.0 * 0.3
    )
    bass_energy = spectral.sub + spectral.bass + spectral.low_mid * 0.35
    mids_energy = spectral.low_mid * 0.65 + spectral.mid + spectral.high_mid * 0.35
    treble_energy = spectral.high_mid * 0.65 + spectral.treble + spectral.air
    tonal_sum = max(1e-12, bass_energy + mids_energy + treble_energy)
    bass = _clamp(bass_energy / tonal_sum)
    mids = _clamp(mids_energy / tonal_sum)
    treble = _clamp(treble_energy / tonal_sum)
    warmth = 0.5 if effectively_silent else _clamp((bass + mids) * 0.7 + (1.0 - spectral.brightness) * 0.3)
    transient_rate = transients / max(duration, 1e-9)
    bass_tightness = 0.0 if effectively_silent else _clamp(
        (1.0 - spectral.sub) * 0.55 + min(transient_rate, 8.0) / 8.0 * 0.45
    )
    # Without the corresponding DI this is deliberately a proxy.  A future paired
    # analyzer may replace it while retaining this schema and normalized range.
    gain = 0.0 if effectively_silent else _clamp(compression * 0.55 + (spectral.high_mid + spectral.treble) * 0.9)
    noise = _clamp(silence_ratio * (10.0 ** (max(rms_dbfs, -60.0) / 20.0)) * 4.0)

    return AudioFingerprint(
        schema_version=FINGERPRINT_SCHEMA_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        asset_sha256=asset_digest,
        audio_sha256=audio_sha256,
        source_kind=source_kind,
        sample_rate_hz=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        duration_seconds=float(duration),
        rms_dbfs=float(rms_dbfs),
        peak_dbfs=float(peak_dbfs),
        clipped_sample_ratio=float(clipped / max(1, sample_count)),
        silence_ratio=float(silence_ratio),
        zero_crossing_rate=float(zero_crossings / max(1, frame_count - 1)),
        spectral=spectral,
        dynamics=DynamicFeatures(
            crest_factor_db=float(crest),
            block_dynamic_range_db=float(min(120.0, dynamic_range)),
            transient_density_hz=float(transient_rate),
            pick_sensitivity=float(pick_sensitivity),
            compression=float(compression),
        ),
        tone=ToneFeatures(
            gain=float(gain),
            bass=float(bass),
            mids=float(mids),
            treble=float(treble),
            warmth=float(warmth),
            bass_tightness=float(bass_tightness),
            noise=float(noise),
        ),
    )


def _plain_intent(intent: Any) -> dict[str, Any]:
    if isinstance(intent, Mapping):
        return dict(intent)
    if hasattr(intent, "model_dump"):
        value = intent.model_dump(mode="python")
        if isinstance(value, dict):
            return value
    raise TypeError("intent must be a mapping or a Pydantic model")


def _nested(data: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                break
            current = current[key]
        else:
            return current
    return None


def _normalized_target(value: Any, labels: Mapping[str, float] | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return _clamp(float(value))
    if isinstance(value, str) and labels:
        return labels.get(value.strip().lower().replace("-", "_"))
    return None


def _relative_spectrum_target(value: Any, baseline: float) -> float | None:
    """Map ToneIntent's signed relative EQ target to an energy-ratio target."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or not -1.0 <= numeric <= 1.0:
        return None
    return _clamp(baseline + baseline * numeric)


DEFAULT_DISTANCE_WEIGHTS = {
    "gain": 1.5,
    "compression": 1.0,
    "pick_sensitivity": 1.2,
    "bass": 0.8,
    "mids": 0.8,
    "treble": 0.8,
    "warmth": 0.8,
    "brightness": 1.0,
    "bass_tightness": 1.0,
    "noise": 0.5,
}


def distance_to_tone_intent(
    fingerprint: AudioFingerprint,
    intent: Any,
    *,
    weights: Mapping[str, float] | None = None,
) -> FingerprintDistance:
    """Weighted normalized distance from measured features to a ToneIntent.

    It accepts the versioned Pydantic ToneIntent as well as its JSON dictionary.
    Unknown or qualitative-only fields are ignored rather than guessed.
    """
    data = _plain_intent(intent)
    targets = {
        "gain": _normalized_target(
            _nested(data, ("gain", "amount"), ("gain",)),
            {"clean": 0.05, "edge_of_breakup": 0.25, "crunch": 0.45, "high_gain": 0.9},
        ),
        "compression": _normalized_target(_nested(data, ("dynamics", "compression"), ("compression",))),
        "pick_sensitivity": _normalized_target(_nested(data, ("dynamics", "pick_sensitivity"), ("pick_sensitivity",))),
        "bass": _relative_spectrum_target(_nested(data, ("spectrum", "bass"), ("bass",)), 0.33),
        "mids": _relative_spectrum_target(_nested(data, ("spectrum", "mids"), ("mids",)), 0.34),
        "treble": _relative_spectrum_target(_nested(data, ("spectrum", "treble"), ("treble",)), 0.33),
        "brightness": _normalized_target(_nested(data, ("spectrum", "brightness"), ("brightness",))),
        "warmth": _normalized_target(_nested(data, ("spectrum", "warmth"), ("warmth",))),
        "bass_tightness": _normalized_target(_nested(data, ("spectrum", "bass_tightness"), ("bass_tightness",))),
        "noise": _normalized_target(_nested(data, ("dynamics", "noise_tolerance"), ("noise",))),
    }
    measured = {
        "gain": fingerprint.tone.gain,
        "compression": fingerprint.dynamics.compression,
        "pick_sensitivity": fingerprint.dynamics.pick_sensitivity,
        "bass": fingerprint.tone.bass,
        "mids": fingerprint.tone.mids,
        "treble": fingerprint.tone.treble,
        "brightness": fingerprint.spectral.brightness,
        "warmth": fingerprint.tone.warmth,
        "bass_tightness": fingerprint.tone.bass_tightness,
        "noise": fingerprint.tone.noise,
    }
    effective_weights = dict(DEFAULT_DISTANCE_WEIGHTS)
    if weights:
        for key, value in weights.items():
            if key not in effective_weights or not math.isfinite(float(value)) or float(value) < 0.0:
                raise ValueError(f"Invalid distance weight: {key}")
            effective_weights[key] = float(value)
    components: dict[str, float] = {}
    weighted_sum = 0.0
    weight_sum = 0.0
    for key, target in targets.items():
        if target is None:
            continue
        difference = abs(measured[key] - target)
        components[key] = float(difference)
        weight = effective_weights[key]
        weighted_sum += weight * difference * difference
        weight_sum += weight
    total = math.sqrt(weighted_sum / weight_sum) if weight_sum else 0.0
    return FingerprintDistance(total=float(total), compared_features=len(components), components=components)


class FingerprintIndex:
    """JSON index keyed by asset SHA-256, persisted with fsync + atomic replace."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: dict[str, AudioFingerprint] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._items = {}
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema_version") != INDEX_SCHEMA_VERSION:
                raise ValueError("Unsupported fingerprint index schema")
            encoded = raw.get("fingerprints")
            if not isinstance(encoded, dict):
                raise ValueError("Invalid fingerprint index")
            parsed: dict[str, AudioFingerprint] = {}
            for digest, value in encoded.items():
                fingerprint = AudioFingerprint.model_validate(value)
                if digest != fingerprint.asset_sha256:
                    raise ValueError("Fingerprint index key does not match asset_sha256")
                parsed[digest] = fingerprint
            self._items = parsed

    def get(self, asset_sha256: str) -> AudioFingerprint | None:
        with self._lock:
            return self._items.get(asset_sha256)

    def values(self) -> list[AudioFingerprint]:
        with self._lock:
            return [self._items[key] for key in sorted(self._items)]

    def put(self, fingerprint: AudioFingerprint) -> None:
        with self._lock:
            self._items[fingerprint.asset_sha256] = fingerprint
            self._persist()

    def remove(self, asset_sha256: str) -> bool:
        with self._lock:
            removed = self._items.pop(asset_sha256, None) is not None
            if removed:
                self._persist()
            return removed

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "fingerprints": {
                digest: self._items[digest].model_dump(mode="json")
                for digest in sorted(self._items)
            },
        }
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary:
                json.dump(payload, temporary, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Directory fsync is not supported on every platform/filesystem.
                pass
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
