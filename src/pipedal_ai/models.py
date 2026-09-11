from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator, model_validator


FiniteNumber = Annotated[StrictInt | StrictFloat, Field(allow_inf_nan=False)]
ParameterValue = StrictBool | FiniteNumber


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class CatalogRef(StrictModel):
    revision: Annotated[int, Field(strict=True, ge=1)]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResourceBinding(StrictModel):
    role: Literal["nam_model", "cab_ir", "reverb_ir"]
    asset_id: str = Field(pattern=r"^ast_[a-f0-9]{24}$")


class ChainStep(StrictModel):
    instance_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    plugin_id: str = Field(pattern=r"^plg_[a-f0-9]{24}$")
    bypass: StrictBool = False
    parameters: dict[str, ParameterValue] = Field(default_factory=dict, max_length=64)
    resources: list[ResourceBinding] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def unique_roles(self) -> "ChainStep":
        roles = [resource.role for resource in self.resources]
        if len(roles) != len(set(roles)):
            raise ValueError("resource roles must be unique per plugin")
        return self


class PresetSpec(StrictModel):
    schema_version: Literal["pipedal-ai.preset-spec/1.0.0"]
    catalog: CatalogRef
    variant: Literal["conservative", "balanced", "bold"]
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=500)
    chain: list[ChainStep] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_instances(self) -> "PresetSpec":
        identifiers = [step.instance_id for step in self.chain]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("instance_id values must be unique")
        return self


class ProposalSet(StrictModel):
    schema_version: Literal["pipedal-ai.proposal-set/1.0.0"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    catalog: CatalogRef
    proposals: list[PresetSpec] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def coherent_set(self) -> "ProposalSet":
        expected = ["conservative", "balanced", "bold"]
        if [proposal.variant for proposal in self.proposals] != expected:
            raise ValueError("variants must be ordered conservative, balanced, bold")
        if any(proposal.catalog != self.catalog for proposal in self.proposals):
            raise ValueError("all proposals must reference the ProposalSet catalog")
        return self


class GuitarProfileCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    guitar: str = Field(default="", max_length=120)
    pickup: Literal["unknown", "single_coil", "humbucker", "p90", "active", "piezo"] = "unknown"
    input_trim_db: FiniteNumber = Field(default=0, ge=-24, le=24)
    notes: str = Field(default="", max_length=500)


class GuitarProfile(GuitarProfileCreate):
    profile_id: str
    created_at: str


class TextPresetJobRequest(StrictModel):
    prompt: str = Field(min_length=3, max_length=2000)
    profile_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    auto_import: StrictBool = False
    auto_activate: StrictBool = False

    @field_validator("prompt")
    @classmethod
    def clean_prompt(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("prompt must contain at least three non-whitespace characters")
        return value

    @model_validator(mode="after")
    def activation_requires_import(self) -> "TextPresetJobRequest":
        if self.auto_activate and not self.auto_import:
            raise ValueError("auto_activate requires auto_import")
        return self


class RTXProposalRequest(StrictModel):
    schema_version: Literal["pipedal-ai.rtx-request/1.0.0"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    prompt: str = Field(min_length=3, max_length=2000)
    profile: dict | None = None
    capabilities: dict

    @field_validator("prompt")
    @classmethod
    def clean_prompt(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("prompt must contain at least three non-whitespace characters")
        return value

    @field_validator("capabilities")
    @classmethod
    def capability_shape(cls, value: dict) -> dict:
        if value.get("schema_version") != "pipedal-ai.catalog-capabilities/1.0.0":
            raise ValueError("unsupported capability schema")
        return value


class ToneIntentRequest(StrictModel):
    prompt: str = Field(min_length=3, max_length=2000)
    profile: dict | None = None

    @field_validator("prompt")
    @classmethod
    def clean_prompt(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("prompt must contain at least three non-whitespace characters")
        return value


class JobView(StrictModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    source: Literal["rtx", "degraded", "none"]
    prompt: str
    catalog: CatalogRef
    created_at: str
    updated_at: str
    error: str | None = None
    artifacts: list[dict] = Field(default_factory=list)
