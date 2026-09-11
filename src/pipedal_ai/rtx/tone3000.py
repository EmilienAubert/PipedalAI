"""Read-only, asynchronous client for the documented TONE3000 v1 API.

This module deliberately exposes metadata operations only.  It never downloads a
model and never writes to PiPedal.  A local asset may be enriched only when it
already carries the exact TONE3000 tone and model identifiers in its provenance.
"""

from __future__ import annotations

from copy import deepcopy
from enum import Enum
from typing import Any, Generic, Literal, Mapping, Sequence, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


DEFAULT_BASE_URL = "https://www.tone3000.com/api/v1"


class Tone3000Error(RuntimeError):
    """Base error for failures talking to TONE3000."""


class Tone3000AuthenticationError(Tone3000Error):
    """The supplied Bearer credential was rejected or lacks access."""


class Tone3000RateLimitError(Tone3000Error):
    """TONE3000 refused a request because its rate limit was reached."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class Tone3000APIError(Tone3000Error):
    """TONE3000 returned a non-success HTTP response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"TONE3000 returned HTTP {status_code}: {message}")
        self.status_code = status_code


class Tone3000TimeoutError(Tone3000Error):
    """A TONE3000 request exceeded its configured timeout."""


class Tone3000TransportError(Tone3000Error):
    """A TONE3000 request could not be completed at the HTTP transport layer."""


class Tone3000ProtocolError(Tone3000Error):
    """TONE3000 returned malformed JSON or a response outside its documented schema."""


class Gear(str, Enum):
    AMP = "amp"
    AMP_CAB = "amp-cab"
    PEDAL = "pedal"
    OUTBOARD = "outboard"
    CAB = "cab"
    SPACE = "space"
    EXPERIMENTAL = "experimental"


class Architecture(str, Enum):
    A1 = "1"
    A2 = "2"
    CUSTOM = "custom"


class ToneFormat(str, Enum):
    NAM = "nam"
    IR = "ir"
    AIDA_X = "aida-x"
    AA_SNAPSHOT = "aa-snapshot"
    PROTEUS = "proteus"


class License(str, Enum):
    T3K = "t3k"
    CC_BY = "cc-by"
    CC_BY_SA = "cc-by-sa"
    CC_BY_NC = "cc-by-nc"
    CC_BY_NC_SA = "cc-by-nc-sa"
    CC_BY_ND = "cc-by-nd"
    CC_BY_NC_ND = "cc-by-nc-nd"
    CC0 = "cco"


class ModelSize(str, Enum):
    STANDARD = "standard"
    LITE = "lite"
    FEATHER = "feather"
    NANO = "nano"
    CUSTOM = "custom"


class ToneSort(str, Enum):
    BEST_MATCH = "best-match"
    NEWEST = "newest"
    OLDEST = "oldest"
    TRENDING = "trending"
    DOWNLOADS_ALL_TIME = "downloads-all-time"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmbeddedUser(_StrictModel):
    id: int = Field(gt=0)
    username: str = Field(min_length=1)
    display_name: str | None
    is_verified: bool
    avatar_url: str | None
    url: str = Field(min_length=1)


class Make(_StrictModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1)


class Tag(_StrictModel):
    id: int = Field(gt=0)
    name: str = Field(min_length=1)


class Tone(_StrictModel):
    id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    user: EmbeddedUser
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    published_at: str | None
    title: str = Field(min_length=1)
    description: str | None
    gear: Gear
    images: list[str] | None
    is_public: bool | None
    links: list[str] | None
    format: ToneFormat
    license: License
    sizes: list[ModelSize]
    makes: list[Make]
    tags: list[Tag]
    models_count: int = Field(ge=0)
    a1_models_count: int = Field(ge=0)
    a2_models_count: int = Field(ge=0)
    irs_count: int = Field(ge=0)
    custom_models_count: int = Field(ge=0)
    downloads_count: int = Field(ge=0)
    favorites_count: int = Field(ge=0)
    is_favorite: bool
    url: str = Field(min_length=1)


class Model(_StrictModel):
    id: int = Field(gt=0)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    user_id: int = Field(gt=0)
    model_url: str = Field(min_length=1)
    name: str = Field(min_length=1)
    size: ModelSize
    tone_id: int = Field(gt=0)
    architecture_version: Architecture | None


ItemT = TypeVar("ItemT")


class Page(_StrictModel, Generic[ItemT]):
    data: list[ItemT]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total: int = Field(ge=0)
    total_pages: int = Field(ge=0)


class Tone3000Provenance(_StrictModel):
    source: Literal["tone3000"]
    tone_id: int = Field(gt=0)
    model_id: int = Field(gt=0)


class Tone3000AssetMetadata(_StrictModel):
    schema_version: Literal["pipedal-ai.tone3000-metadata/1.0.0"]
    tone_id: int
    model_id: int
    title: str
    description: str | None
    gear: Gear
    format: ToneFormat
    license: License
    makes: list[str]
    tags: list[str]
    creator_username: str
    creator_display_name: str
    creator_verified: bool
    model_name: str
    model_size: ModelSize
    architecture: Architecture | None
    source_url: str


class Tone3000Client:
    """Small async client limited to TONE3000 metadata endpoints.

    Passing an ``http_client`` is useful for dependency injection and testing.  An
    injected client remains owned by the caller and is therefore not closed here.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        token = token.strip()
        if not token:
            raise ValueError("A non-empty TONE3000 access token is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        self._authorization = f"Bearer {token}"

    async def __aenter__(self) -> Tone3000Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_tone(
        self, tone_id: int, *, architecture: Architecture | None = None
    ) -> Tone:
        _positive("tone_id", tone_id)
        params = {"architecture": architecture.value} if architecture else None
        return await self._get(f"/tones/{tone_id}", Tone, params=params)

    async def list_models(
        self,
        tone_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        architecture: Architecture | None = None,
    ) -> Page[Model]:
        _positive("tone_id", tone_id)
        _page_values(page, page_size, maximum=300)
        params: dict[str, Any] = {
            "tone_id": tone_id,
            "page": page,
            "page_size": page_size,
        }
        if architecture:
            params["architecture"] = architecture.value
        return await self._get("/models", Page[Model], params=params)

    async def search_tones(
        self,
        query: str = "",
        *,
        page: int = 1,
        page_size: int = 10,
        sort: ToneSort | None = None,
        gears: Sequence[Gear] = (),
        sizes: Sequence[ModelSize] = (),
        tags: Sequence[str] = (),
        makes: Sequence[str] = (),
        creators: Sequence[str] = (),
        format: ToneFormat | None = None,
        architecture: Architecture | None = None,
        calibrated: bool | None = None,
        verified: bool | None = None,
    ) -> Page[Tone]:
        _page_values(page, page_size, maximum=25)
        params: dict[str, Any] = {"query": query, "page": page, "page_size": page_size}
        if sort:
            params["sort"] = sort.value
        if gears:
            params["gears"] = "_".join(value.value for value in gears)
        if sizes:
            # TONE3000's canonical v1 encoding for multiple sizes is hyphen-separated.
            params["sizes"] = "-".join(value.value for value in sizes)
        if tags:
            params["tags"] = "_".join(_non_empty_values("tags", tags))
        if makes:
            params["makes"] = "_".join(_non_empty_values("makes", makes))
        if creators:
            params["creators"] = ",".join(_non_empty_values("creators", creators))
        if format:
            params["format"] = format.value
        if architecture:
            params["architecture"] = architecture.value
        if calibrated is not None:
            params["calibrated"] = str(calibrated).lower()
        if verified is not None:
            params["verified"] = str(verified).lower()
        return await self._get("/tones/search", Page[Tone], params=params)

    async def _get(
        self,
        path: str,
        response_type: Any,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=params,
                headers={"Authorization": self._authorization, "Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise Tone3000TimeoutError("TONE3000 request timed out") from exc
        except httpx.RequestError as exc:
            raise Tone3000TransportError("TONE3000 request failed") from exc

        if response.status_code == 429:
            raise Tone3000RateLimitError(
                "TONE3000 rate limit reached",
                retry_after_seconds=_retry_after(response.headers.get("Retry-After")),
            )
        if response.status_code in (401, 403):
            raise Tone3000AuthenticationError(
                f"TONE3000 rejected the credential or access (HTTP {response.status_code})"
            )
        if response.is_error:
            raise Tone3000APIError(response.status_code, _error_message(response))

        try:
            # JSON-aware strict validation accepts the documented string enums while
            # still rejecting Python-side coercions and unknown response fields.
            return TypeAdapter(response_type).validate_json(response.content, strict=True)
        except (ValueError, ValidationError) as exc:
            raise Tone3000ProtocolError("Invalid TONE3000 JSON response") from exc


def merge_tone3000_metadata(
    asset: Mapping[str, Any], *, tone: Tone, model: Model
) -> dict[str, Any]:
    """Return a copy of ``asset`` enriched from an exact provenance relationship.

    The asset must already contain ``provenance.source``, ``tone_id`` and
    ``model_id``.  Names, paths and hashes are intentionally never used to infer a
    relationship.
    """

    try:
        provenance = Tone3000Provenance.model_validate(asset.get("provenance"), strict=True)
    except ValidationError as exc:
        raise ValueError(
            "Asset needs explicit TONE3000 provenance with tone_id and model_id"
        ) from exc
    if provenance.tone_id != tone.id:
        raise ValueError("Asset provenance tone_id does not match the supplied tone")
    if provenance.model_id != model.id:
        raise ValueError("Asset provenance model_id does not match the supplied model")
    if model.tone_id != tone.id:
        raise ValueError("The supplied TONE3000 model does not belong to the supplied tone")

    result = deepcopy(dict(asset))
    raw_metadata = result.get("metadata", {})
    if not isinstance(raw_metadata, Mapping):
        raise ValueError("Asset metadata must be an object")
    metadata = deepcopy(dict(raw_metadata))
    display_name = tone.user.display_name or tone.user.username
    enrichment = Tone3000AssetMetadata(
        schema_version="pipedal-ai.tone3000-metadata/1.0.0",
        tone_id=tone.id,
        model_id=model.id,
        title=tone.title,
        description=tone.description,
        gear=tone.gear,
        format=tone.format,
        license=tone.license,
        makes=[value.name for value in tone.makes],
        tags=[value.name for value in tone.tags],
        creator_username=tone.user.username,
        creator_display_name=display_name,
        creator_verified=tone.user.is_verified,
        model_name=model.name,
        model_size=model.size,
        architecture=model.architecture_version,
        source_url=tone.url,
    )
    metadata["tone3000"] = enrichment.model_dump(mode="json")
    result["metadata"] = metadata
    return result


def _positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _page_values(page: int, page_size: int, *, maximum: int) -> None:
    _positive("page", page)
    _positive("page_size", page_size)
    if page_size > maximum:
        raise ValueError(f"page_size must not exceed {maximum}")


def _non_empty_values(name: str, values: Sequence[str]) -> list[str]:
    normalized = [value.strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError(f"{name} cannot contain empty values")
    return normalized


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:200] if text else "request failed"
    if isinstance(payload, Mapping):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return "request failed"
