from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from pydantic import ValidationError

from pipedal_ai.rtx.fingerprints import (
    AudioFingerprint,
    FingerprintIndex,
    distance_to_tone_intent,
    extract_wav_fingerprint,
)


def write_tone(path: Path, frequency: float, *, channels: int = 1, amplitude: float = 0.5, duration: float = 0.5) -> None:
    sample_rate = 16000
    frames = []
    for index in range(round(sample_rate * duration)):
        value = int(32767 * amplitude * math.sin(2.0 * math.pi * frequency * index / sample_rate))
        frames.append(struct.pack("<h", value) * channels)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"".join(frames))


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_extracts_pcm_mono_and_distinguishes_frequency_bands(self):
        low_path = self.root / "low.wav"
        high_path = self.root / "high.wav"
        write_tone(low_path, 120.0)
        write_tone(high_path, 4000.0)
        low = extract_wav_fingerprint(low_path, source_kind="render")
        high = extract_wav_fingerprint(high_path, source_kind="render")

        self.assertEqual(low.schema_version, "pipedal-ai.audio-fingerprint/1.0.0")
        self.assertEqual(low.sample_rate_hz, 16000)
        self.assertEqual(low.channels, 1)
        self.assertAlmostEqual(low.duration_seconds, 0.5, places=3)
        self.assertAlmostEqual(low.rms_dbfs, -9.03, delta=0.15)
        self.assertAlmostEqual(low.peak_dbfs, -6.02, delta=0.05)
        self.assertGreater(low.spectral.bass, high.spectral.bass)
        self.assertGreater(high.spectral.brightness, low.spectral.brightness)
        self.assertGreater(high.spectral.centroid_hz, low.spectral.centroid_hz)

    def test_extracts_stereo_and_honors_asset_hash(self):
        path = self.root / "stereo.wav"
        write_tone(path, 440.0, channels=2)
        digest = "a" * 64
        fingerprint = extract_wav_fingerprint(path, asset_sha256=digest, source_kind="nam")
        self.assertEqual(fingerprint.channels, 2)
        self.assertEqual(fingerprint.asset_sha256, digest)
        self.assertNotEqual(fingerprint.audio_sha256, digest)

    def test_schema_is_strict_and_forbids_unknown_fields(self):
        path = self.root / "tone.wav"
        write_tone(path, 440.0)
        payload = extract_wav_fingerprint(path).model_dump(mode="python")
        payload["unexpected"] = True
        with self.assertRaises(ValidationError):
            AudioFingerprint.model_validate(payload)

    def test_silence_does_not_look_like_compressed_high_gain_audio(self):
        path = self.root / "silence.wav"
        with wave.open(str(path), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(8000)
            target.writeframes(b"\0" * 2 * 800)
        fingerprint = extract_wav_fingerprint(path)
        self.assertEqual(fingerprint.rms_dbfs, -120.0)
        self.assertEqual(fingerprint.silence_ratio, 1.0)
        self.assertEqual(fingerprint.tone.gain, 0.0)
        self.assertEqual(fingerprint.dynamics.compression, 0.0)

    def test_distance_uses_available_tone_intent_features(self):
        low_path = self.root / "low.wav"
        high_path = self.root / "high.wav"
        write_tone(low_path, 120.0)
        write_tone(high_path, 4000.0)
        low = extract_wav_fingerprint(low_path)
        high = extract_wav_fingerprint(high_path)
        bright_intent = {
            "gain": {"amount": high.tone.gain},
            "dynamics": {"compression": high.dynamics.compression},
            "spectrum": {"brightness": 1.0, "bass": 0.0, "treble": 1.0},
        }
        low_distance = distance_to_tone_intent(low, bright_intent)
        high_distance = distance_to_tone_intent(high, bright_intent)
        self.assertEqual(high_distance.compared_features, 5)
        self.assertLess(high_distance.total, low_distance.total)

    def test_index_round_trip_is_keyed_by_asset_hash(self):
        wav_path = self.root / "tone.wav"
        index_path = self.root / "index" / "fingerprints.json"
        write_tone(wav_path, 440.0)
        fingerprint = extract_wav_fingerprint(wav_path, asset_sha256="b" * 64)

        index = FingerprintIndex(index_path)
        index.put(fingerprint)
        self.assertTrue(index_path.exists())
        loaded = FingerprintIndex(index_path)
        self.assertEqual(loaded.get("b" * 64), fingerprint)
        self.assertEqual(loaded.values(), [fingerprint])
        self.assertTrue(loaded.remove("b" * 64))
        self.assertIsNone(FingerprintIndex(index_path).get("b" * 64))

    def test_rejects_non_pcm_or_unsupported_channel_count(self):
        path = self.root / "surround.wav"
        with wave.open(str(path), "wb") as target:
            target.setnchannels(3)
            target.setsampwidth(2)
            target.setframerate(8000)
            target.writeframes(b"\0" * 3 * 2 * 8)
        with self.assertRaisesRegex(ValueError, "mono and stereo"):
            extract_wav_fingerprint(path)


if __name__ == "__main__":
    unittest.main()
