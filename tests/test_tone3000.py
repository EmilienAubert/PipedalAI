import asyncio
import json
import unittest

import httpx

from pipedal_ai.rtx.tone3000 import (
    Architecture,
    Gear,
    Model,
    ModelSize,
    Tone,
    Tone3000Client,
    Tone3000ProtocolError,
    Tone3000RateLimitError,
    Tone3000TimeoutError,
    ToneFormat,
    ToneSort,
    merge_tone3000_metadata,
)


def _tone_payload() -> dict:
    return {
        "id": 42,
        "user_id": 7,
        "user": {
            "id": 7,
            "username": "builder",
            "display_name": "Verified Builder",
            "is_verified": True,
            "avatar_url": None,
            "url": "https://www.tone3000.com/users/builder",
        },
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "published_at": "2026-01-02T00:00:00Z",
        "title": "Tweed edge",
        "description": "Dynamic edge-of-breakup capture",
        "gear": "amp-cab",
        "images": [],
        "is_public": True,
        "links": None,
        "format": "nam",
        "license": "t3k",
        "sizes": ["standard"],
        "makes": [{"id": 3, "name": "Fender Deluxe"}],
        "tags": [{"id": 5, "name": "crunch"}],
        "models_count": 1,
        "a1_models_count": 0,
        "a2_models_count": 1,
        "irs_count": 0,
        "custom_models_count": 0,
        "downloads_count": 12,
        "favorites_count": 4,
        "is_favorite": False,
        "url": "https://www.tone3000.com/tones/42",
    }


def _model_payload() -> dict:
    return {
        "id": 99,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "user_id": 7,
        "model_url": "https://www.tone3000.com/api/v1/models/99/download",
        "name": "Volume 5",
        "size": "standard",
        "tone_id": 42,
        "architecture_version": "2",
    }


def _page(data: list[dict]) -> dict:
    return {"data": data, "page": 1, "page_size": 25, "total": len(data), "total_pages": 1}


class Tone3000ClientTests(unittest.TestCase):
    def test_get_tone_injects_bearer_token_and_architecture(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.headers["Authorization"], "Bearer secret-token")
                self.assertEqual(request.url.path, "/api/v1/tones/42")
                self.assertEqual(request.url.params["architecture"], "2")
                return httpx.Response(200, json=_tone_payload())

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as http_client:
                client = Tone3000Client("secret-token", http_client=http_client)
                tone = await client.get_tone(42, architecture=Architecture.A2)
                self.assertEqual(tone.title, "Tweed edge")
                self.assertEqual(tone.gear, Gear.AMP_CAB)

        asyncio.run(run())

    def test_list_models_is_metadata_only(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/api/v1/models")
                self.assertEqual(request.url.params["tone_id"], "42")
                self.assertEqual(request.method, "GET")
                return httpx.Response(200, json=_page([_model_payload()]))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                page = await Tone3000Client("token", http_client=http_client).list_models(
                    42, page_size=25
                )
                self.assertEqual(page.data[0].id, 99)
                self.assertEqual(page.data[0].architecture_version, Architecture.A2)

        asyncio.run(run())

    def test_search_serializes_documented_filters(self) -> None:
        async def run() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                params = request.url.params
                self.assertEqual(request.url.path, "/api/v1/tones/search")
                self.assertEqual(params["query"], "warm crunch")
                self.assertEqual(params["gears"], "amp_amp-cab")
                self.assertEqual(params["sizes"], "standard-lite")
                self.assertEqual(params["tags"], "warm_crunch")
                self.assertEqual(params["creators"], "alice,bob_name")
                self.assertEqual(params["format"], "nam")
                self.assertEqual(params["verified"], "true")
                return httpx.Response(200, json=_page([_tone_payload()]))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                result = await Tone3000Client("token", http_client=http_client).search_tones(
                    "warm crunch",
                    page_size=25,
                    sort=ToneSort.BEST_MATCH,
                    gears=(Gear.AMP, Gear.AMP_CAB),
                    sizes=(ModelSize.STANDARD, ModelSize.LITE),
                    tags=("warm", "crunch"),
                    creators=("alice", "bob_name"),
                    format=ToneFormat.NAM,
                    verified=True,
                )
                self.assertEqual([tone.id for tone in result.data], [42])

        asyncio.run(run())

    def test_rate_limit_exposes_retry_after_without_sleeping(self) -> None:
        async def run() -> None:
            transport = httpx.MockTransport(
                lambda _: httpx.Response(429, headers={"Retry-After": "3.5"}, json={})
            )
            async with httpx.AsyncClient(transport=transport) as http_client:
                with self.assertRaises(Tone3000RateLimitError) as raised:
                    await Tone3000Client("token", http_client=http_client).get_tone(42)
                self.assertEqual(raised.exception.retry_after_seconds, 3.5)

        asyncio.run(run())

    def test_timeout_and_invalid_schema_have_clean_errors(self) -> None:
        async def run_timeout() -> None:
            def handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ReadTimeout("late", request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                with self.assertRaises(Tone3000TimeoutError):
                    await Tone3000Client("token", http_client=http_client).get_tone(42)

        async def run_schema() -> None:
            transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"id": 42}))
            async with httpx.AsyncClient(transport=transport) as http_client:
                with self.assertRaises(Tone3000ProtocolError):
                    await Tone3000Client("token", http_client=http_client).get_tone(42)

        asyncio.run(run_timeout())
        asyncio.run(run_schema())


class Tone3000EnrichmentTests(unittest.TestCase):
    def test_merges_only_an_exact_explicit_provenance(self) -> None:
        tone = Tone.model_validate_json(json.dumps(_tone_payload()), strict=True)
        model = Model.model_validate_json(json.dumps(_model_payload()), strict=True)
        asset = {
            "sha256": "a" * 64,
            "provenance": {"source": "tone3000", "tone_id": 42, "model_id": 99},
            "metadata": {"local_note": "kept"},
        }

        enriched = merge_tone3000_metadata(asset, tone=tone, model=model)

        self.assertNotIn("tone3000", asset["metadata"])
        self.assertEqual(enriched["metadata"]["local_note"], "kept")
        self.assertEqual(enriched["metadata"]["tone3000"]["model_id"], 99)
        self.assertEqual(enriched["metadata"]["tone3000"]["tags"], ["crunch"])
        self.assertNotIn("model_url", enriched["metadata"]["tone3000"])

    def test_refuses_name_matching_or_mismatched_identifiers(self) -> None:
        tone = Tone.model_validate_json(json.dumps(_tone_payload()), strict=True)
        model = Model.model_validate_json(json.dumps(_model_payload()), strict=True)
        with self.assertRaisesRegex(ValueError, "explicit TONE3000 provenance"):
            merge_tone3000_metadata({"name": "Volume 5"}, tone=tone, model=model)
        with self.assertRaisesRegex(ValueError, "model_id"):
            merge_tone3000_metadata(
                {
                    "provenance": {
                        "source": "tone3000",
                        "tone_id": 42,
                        "model_id": 100,
                    }
                },
                tone=tone,
                model=model,
            )


if __name__ == "__main__":
    unittest.main()
