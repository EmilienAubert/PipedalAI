from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


TONE_INTENT_SCHEMA_VERSION = "pipedal-ai.tone-intent/1.0.0"

UnitFloat = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
SignedUnitFloat = Annotated[float, Field(strict=True, ge=-1.0, le=1.0, allow_inf_nan=False)]

EffectRole = Literal[
    "input",
    "noise_gate",
    "compressor",
    "pitch",
    "wah",
    "drive",
    "amp",
    "cabinet",
    "eq",
    "modulation",
    "delay",
    "reverb",
    "output",
]


class IntentModel(BaseModel):
    """Strict base shared by every object exposed in the ToneIntent contract."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class GainIntent(IntentModel):
    character: Literal[
        "clean",
        "edge_of_breakup",
        "crunch",
        "overdrive",
        "distortion",
        "high_gain",
        "fuzz",
    ] = "clean"
    amount: UnitFloat = 0.0
    saturation_softness: UnitFloat = 0.7


class DynamicsIntent(IntentModel):
    compression: UnitFloat = 0.15
    pick_sensitivity: UnitFloat = 0.8
    sustain: UnitFloat = 0.35
    transient_attack: UnitFloat = 0.75
    noise_tolerance: UnitFloat = 0.2


class SpectrumIntent(IntentModel):
    """Relative tonal targets; zero means neutral, not silence."""

    bass: SignedUnitFloat = 0.0
    low_mids: SignedUnitFloat = 0.0
    mids: SignedUnitFloat = 0.0
    high_mids: SignedUnitFloat = 0.0
    treble: SignedUnitFloat = 0.0
    warmth: UnitFloat = 0.5
    brightness: UnitFloat = 0.5
    bass_tightness: UnitFloat = 0.5


class SpaceIntent(IntentModel):
    enabled: StrictBool = False
    kind: Literal["dry", "room", "spring", "plate", "hall", "ambient"] = "dry"
    mix: UnitFloat = 0.0
    size: UnitFloat = 0.25
    decay: UnitFloat = 0.2
    pre_delay: UnitFloat = 0.0

    @model_validator(mode="after")
    def coherent_space(self) -> "SpaceIntent":
        if not self.enabled and (self.kind != "dry" or self.mix != 0.0):
            raise ValueError("disabled space must use kind='dry' and mix=0")
        if self.enabled and (self.kind == "dry" or self.mix == 0.0):
            raise ValueError("enabled space requires a non-dry kind and a positive mix")
        return self


class ModulationIntent(IntentModel):
    enabled: StrictBool = False
    kind: Literal[
        "none", "chorus", "flanger", "phaser", "tremolo", "vibrato", "rotary", "detune"
    ] = "none"
    mix: UnitFloat = 0.0
    depth: UnitFloat = 0.0
    rate: UnitFloat = 0.3
    stereo_width: UnitFloat = 0.0

    @model_validator(mode="after")
    def coherent_modulation(self) -> "ModulationIntent":
        if not self.enabled and (self.kind != "none" or self.mix != 0.0 or self.depth != 0.0):
            raise ValueError("disabled modulation must use kind='none', mix=0 and depth=0")
        if self.enabled and (self.kind == "none" or self.mix == 0.0 or self.depth == 0.0):
            raise ValueError("enabled modulation requires a kind, positive mix and positive depth")
        return self


class DelayIntent(IntentModel):
    enabled: StrictBool = False
    kind: Literal["none", "slapback", "analog", "tape", "digital", "multi_tap"] = "none"
    mix: UnitFloat = 0.0
    feedback: UnitFloat = 0.0
    time_ms: Annotated[float, Field(strict=True, ge=1.0, le=4000.0, allow_inf_nan=False)] | None = None
    subdivision: Literal["whole", "half", "quarter", "eighth", "dotted_eighth", "triplet", "sixteenth"] | None = None

    @model_validator(mode="after")
    def coherent_delay(self) -> "DelayIntent":
        if not self.enabled:
            if self.kind != "none" or self.mix != 0.0 or self.feedback != 0.0:
                raise ValueError("disabled delay must use kind='none', mix=0 and feedback=0")
            if self.time_ms is not None or self.subdivision is not None:
                raise ValueError("disabled delay cannot specify timing")
        elif self.kind == "none" or self.mix == 0.0:
            raise ValueError("enabled delay requires a kind and a positive mix")
        elif self.time_ms is None and self.subdivision is None:
            raise ValueError("enabled delay requires time_ms or subdivision")
        return self


class StyleIntent(IntentModel):
    genres: list[str] = Field(default_factory=list, max_length=8)
    era: Literal[
        "unspecified", "1950s", "1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "modern"
    ] = "unspecified"
    character: list[
        Literal[
            "vintage",
            "modern",
            "organic",
            "raw",
            "polished",
            "smooth",
            "aggressive",
            "dark",
            "warm",
            "bright",
            "airy",
        ]
    ] = Field(default_factory=list, max_length=8)

    @field_validator("genres")
    @classmethod
    def valid_genres(cls, value: list[str]) -> list[str]:
        if any(not genre.strip() or len(genre) > 40 for genre in value):
            raise ValueError("genres must contain non-empty strings of at most 40 characters")
        normalized = [genre.strip().lower() for genre in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("genres must be unique")
        return normalized

    @field_validator("character")
    @classmethod
    def unique_character(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("style character values must be unique")
        return value


class ChainConstraints(IntentModel):
    max_plugins: Annotated[int, Field(strict=True, ge=1, le=12)] = 8
    cpu_budget: Literal["low", "balanced", "high"] = "balanced"
    prefer_simple_chain: StrictBool = True
    allow_stereo: StrictBool = True
    allow_nam: StrictBool = True
    allow_cab_ir: StrictBool = True
    required_roles: list[EffectRole] = Field(default_factory=list, max_length=13)
    forbidden_roles: list[EffectRole] = Field(default_factory=list, max_length=13)

    @model_validator(mode="after")
    def coherent_roles(self) -> "ChainConstraints":
        if len(self.required_roles) != len(set(self.required_roles)):
            raise ValueError("required_roles must be unique")
        if len(self.forbidden_roles) != len(set(self.forbidden_roles)):
            raise ValueError("forbidden_roles must be unique")
        overlap = set(self.required_roles) & set(self.forbidden_roles)
        if overlap:
            raise ValueError(f"effect roles cannot be both required and forbidden: {sorted(overlap)}")
        return self


class GuitarContext(IntentModel):
    profile_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = Field(default="", max_length=120)
    pickup: Literal["unknown", "single_coil", "humbucker", "p90", "active", "piezo"] = "unknown"
    pickup_position: Literal["unknown", "bridge", "middle", "neck", "mixed"] = "unknown"
    output_level: Literal["unknown", "low", "medium", "high"] = "unknown"
    input_trim_db: Annotated[float, Field(strict=True, ge=-24.0, le=24.0, allow_inf_nan=False)] = 0.0
    playing_style: Literal["unknown", "pick", "fingers", "hybrid"] = "unknown"


class ToneIntent(IntentModel):
    """Versioned musical target exchanged independently from LV2/NAM choices."""

    schema_version: Literal["pipedal-ai.tone-intent/1.0.0"] = TONE_INTENT_SCHEMA_VERSION
    prompt: str = Field(min_length=3, max_length=2000)
    gain: GainIntent = Field(default_factory=GainIntent)
    dynamics: DynamicsIntent = Field(default_factory=DynamicsIntent)
    spectrum: SpectrumIntent = Field(default_factory=SpectrumIntent)
    space: SpaceIntent = Field(default_factory=SpaceIntent)
    modulation: ModulationIntent = Field(default_factory=ModulationIntent)
    delay: DelayIntent = Field(default_factory=DelayIntent)
    style: StyleIntent = Field(default_factory=StyleIntent)
    chain_constraints: ChainConstraints = Field(default_factory=ChainConstraints)
    guitar: GuitarContext = Field(default_factory=GuitarContext)
    confidence: UnitFloat = 0.5

    @field_validator("prompt")
    @classmethod
    def meaningful_prompt(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("prompt must contain at least three non-whitespace characters")
        return value


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", plain.lower()))


def _has(text: str, *phrases: str) -> bool:
    padded = f" {text} "
    return any(f" {_normalize(phrase)} " in padded for phrase in phrases)


def _profile_context(profile: Mapping[str, Any] | None) -> GuitarContext:
    if not profile:
        return GuitarContext()
    pickup = profile.get("pickup", "unknown")
    if pickup not in {"unknown", "single_coil", "humbucker", "p90", "active", "piezo"}:
        pickup = "unknown"
    try:
        trim = max(-24.0, min(24.0, float(profile.get("input_trim_db", 0.0))))
    except (TypeError, ValueError):
        trim = 0.0
    return GuitarContext(
        profile_id=profile.get("profile_id"),
        description=str(profile.get("guitar", ""))[:120],
        pickup=pickup,
        input_trim_db=trim,
        output_level="high" if pickup in {"active", "humbucker"} else ("low" if pickup == "single_coil" else "unknown"),
    )


def fallback_tone_intent(prompt: str, profile: Mapping[str, Any] | None = None) -> ToneIntent:
    """Return a conservative, deterministic intent when semantic inference is unavailable.

    This deliberately covers common French and English cues only. It is a safe
    degraded-mode contract producer, not a replacement for the RTX extractor.
    """

    text = _normalize(prompt)
    dry = _has(
        text,
        "dry",
        "sec",
        "seche",
        "sans reverb",
        "sans reverberation",
        "sans ambiance",
        "pas de reverb",
        "no reverb",
    )
    no_delay = _has(text, "sans delay", "sans echo", "pas de delay", "pas d echo", "no delay", "no echo")

    if _has(text, "fuzz"):
        gain = GainIntent(character="fuzz", amount=0.85, saturation_softness=0.35)
    elif _has(text, "high gain", "gros gain", "metal", "grosse saturation"):
        gain = GainIntent(character="high_gain", amount=0.85, saturation_softness=0.35)
    elif _has(text, "distortion", "distordu", "distordue"):
        gain = GainIntent(character="distortion", amount=0.7, saturation_softness=0.4)
    elif _has(text, "crunch"):
        gain = GainIntent(character="crunch", amount=0.4, saturation_softness=0.7)
    elif _has(text, "breakup", "a la limite de la saturation", "saturation legere", "legerement sature", "legerement saturee"):
        gain = GainIntent(character="edge_of_breakup", amount=0.25, saturation_softness=0.8)
    elif _has(text, "overdrive", "sature", "saturee"):
        gain = GainIntent(character="overdrive", amount=0.55, saturation_softness=0.6)
    else:
        gain = GainIntent()

    dynamic = _has(text, "dynamique", "dynamic", "reactif", "reactive", "attaque")
    compressed = _has(text, "compresse", "compressee", "compressed")
    dynamics = DynamicsIntent(
        compression=0.65 if compressed else (0.08 if dynamic else 0.15),
        pick_sensitivity=0.92 if dynamic else 0.8,
        sustain=0.7 if _has(text, "sustain", "longue tenue") else 0.35,
        transient_attack=0.9 if dynamic else 0.75,
        noise_tolerance=0.08 if _has(text, "peu de bruit", "silencieux", "silencieuse", "low noise") else 0.2,
    )

    warm = _has(text, "chaud", "chaude", "warm", "rond", "ronde")
    bright = _has(text, "brillant", "brillante", "bright", "cristallin", "cristalline")
    dark = _has(text, "sombre", "dark", "feutre", "feutree")
    tight = _has(text, "graves fermes", "grave ferme", "tight bass", "graves serres", "grave serre")
    present_mids = _has(text, "mediums presents", "medium present", "mids present", "forward mids")
    soft_treble = _has(text, "aigus doux", "aigu doux", "soft treble", "smooth highs")
    spectrum = SpectrumIntent(
        bass=0.15 if warm else 0.0,
        low_mids=0.2 if warm else 0.0,
        mids=0.3 if present_mids else 0.0,
        high_mids=-0.1 if dark else 0.0,
        treble=-0.25 if (dark or soft_treble) else (0.2 if bright else 0.0),
        warmth=0.8 if warm else (0.35 if bright else 0.5),
        brightness=0.25 if dark else (0.8 if bright else (0.35 if soft_treble else 0.5)),
        bass_tightness=0.85 if tight else 0.5,
    )

    space_kind = "dry"
    if not dry:
        for phrase, candidate in (("spring", "spring"), ("ressort", "spring"), ("plate", "plate"), ("hall", "hall"), ("ambient", "ambient"), ("room", "room"), ("piece", "room")):
            if _has(text, phrase):
                space_kind = candidate
                break
        if space_kind == "dry" and _has(text, "reverb", "reverberation", "ambiance"):
            space_kind = "room"
    space_enabled = space_kind != "dry"
    subtle_space = _has(text, "leger", "legere", "petit", "petite", "subtle", "slight")
    space = SpaceIntent(
        enabled=space_enabled,
        kind=space_kind,
        mix=(0.12 if subtle_space else 0.22) if space_enabled else 0.0,
        size=0.3 if space_kind == "room" else (0.75 if space_kind in {"hall", "ambient"} else 0.5),
        decay=0.2 if space_kind == "room" else (0.7 if space_kind in {"hall", "ambient"} else 0.45),
    )

    modulation_kind = "none"
    for phrase, candidate in (("chorus", "chorus"), ("flanger", "flanger"), ("phaser", "phaser"), ("tremolo", "tremolo"), ("vibrato", "vibrato"), ("rotary", "rotary"), ("detune", "detune")):
        if _has(text, phrase):
            modulation_kind = candidate
            break
    modulation_enabled = modulation_kind != "none"
    modulation = ModulationIntent(
        enabled=modulation_enabled,
        kind=modulation_kind,
        mix=0.18 if modulation_enabled else 0.0,
        depth=0.3 if modulation_enabled else 0.0,
        stereo_width=0.5 if modulation_enabled else 0.0,
    )

    delay_kind = "none"
    if not no_delay:
        for phrase, candidate in (("slapback", "slapback"), ("tape delay", "tape"), ("echo a bande", "tape"), ("analog delay", "analog"), ("delay analogique", "analog"), ("digital delay", "digital"), ("delay numerique", "digital"), ("multi tap", "multi_tap"), ("delay", "digital"), ("echo", "digital")):
            if _has(text, phrase):
                delay_kind = candidate
                break
    delay_enabled = delay_kind != "none"
    delay = DelayIntent(
        enabled=delay_enabled,
        kind=delay_kind,
        mix=0.16 if delay_enabled else 0.0,
        feedback=0.18 if delay_kind == "slapback" else (0.3 if delay_enabled else 0.0),
        time_ms=110.0 if delay_kind == "slapback" else (380.0 if delay_enabled else None),
    )

    known_genres = ("blues", "rock", "metal", "jazz", "funk", "country", "pop", "punk", "grunge", "ambient", "soul", "reggae")
    genres = [genre for genre in known_genres if _has(text, genre)]
    era = "unspecified"
    for candidate in ("1950s", "1960s", "1970s", "1980s", "1990s", "2000s", "2010s"):
        decade = candidate[:4]
        if _has(text, candidate, f"annees {decade}", f"{decade}s"):
            era = candidate
            break
    if era == "unspecified" and _has(text, "moderne", "modern"):
        era = "modern"
    character = []
    for candidate, phrases in (
        ("vintage", ("vintage",)),
        ("modern", ("moderne", "modern")),
        ("organic", ("organique", "organic")),
        ("raw", ("brut", "brute", "raw")),
        ("polished", ("produit", "produite", "polished")),
        ("smooth", ("doux", "douce", "smooth")),
        ("aggressive", ("agressif", "agressive", "aggressive")),
        ("dark", ("sombre", "dark")),
        ("warm", ("chaud", "chaude", "warm")),
        ("bright", ("brillant", "brillante", "bright")),
        ("airy", ("aere", "aeree", "airy")),
    ):
        if _has(text, *phrases):
            character.append(candidate)

    simple = _has(text, "simple", "minimal", "legere en cpu", "faible cpu")
    constraints = ChainConstraints(
        max_plugins=5 if simple else 8,
        cpu_budget="low" if simple else "balanced",
        prefer_simple_chain=True,
        forbidden_roles=[role for role, forbidden in (("delay", no_delay), ("reverb", dry)) if forbidden],
    )

    return ToneIntent(
        prompt=prompt,
        gain=gain,
        dynamics=dynamics,
        spectrum=spectrum,
        space=space,
        modulation=modulation,
        delay=delay,
        style=StyleIntent(genres=genres, era=era, character=character),
        chain_constraints=constraints,
        guitar=_profile_context(profile),
        confidence=0.45,
    )
