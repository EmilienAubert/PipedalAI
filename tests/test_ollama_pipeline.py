from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from pipedal_ai.config import OllamaConfig
from pipedal_ai.degraded import DegradedProposer
from pipedal_ai.errors import RemoteServiceError
from pipedal_ai.intent import fallback_tone_intent
from pipedal_ai.models import RTXProposalRequest
from pipedal_ai.rtx.ollama import OllamaClient


CATALOG = {"revision": 3, "sha256": "a" * 64}
INPUT_ID = "plg_111111111111111111111111"


def capabilities() -> dict:
    return {
        "schema_version": "pipedal-ai.catalog-capabilities/1.0.0",
        "catalog": CATALOG,
        "capability_sha256": "b" * 64,
        "plugins": [
            {
                "plugin_id": INPUT_ID,
                "uri": "http://two-play.com/plugins/toob-input_stage",
                "name": "TooB Input Stage",
                "class": "Plugin",
                "audio_inputs": 1,
                "audio_outputs": 1,
                "allowed_in_live_chain": True,
                "resource_roles": [],
                "controls": [
                    {"symbol": "trim", "name": "Trim", "datatype": "number",
                     "minimum": -24.0, "maximum": 12.0, "default": 0.0, "scale_points": []}
                ],
            }
        ],
        "assets": [],
    }


def request(prompt: str = "blues chaud et dynamique") -> RTXProposalRequest:
    return RTXProposalRequest(
        schema_version="pipedal-ai.rtx-request/1.0.0",
        request_id="job_test",
        prompt=prompt,
        profile=None,
        capabilities=capabilities(),
    )


def proposal(plugin_id: str = INPUT_ID) -> dict:
    result = DegradedProposer().propose("job_test", "clean", capabilities()).model_dump(mode="json")
    for preset in result["proposals"]:
        preset["chain"] = [
            {"instance_id": "input", "plugin_id": plugin_id, "bypass": False,
             "parameters": {"trim": -6.0}, "resources": []}
        ]
    return result


class OllamaPipelineTests(unittest.TestCase):
    def config(self, retries: int = 1) -> OllamaConfig:
        return OllamaConfig(
            base_url="http://ollama.test",
            model="test-model",
            timeout_seconds=10,
            temperature=0.0,
            max_retries=retries,
            max_plugin_candidates=24,
            max_assets_per_role=12,
        )

    def test_two_stage_pipeline_uses_only_shortlist_for_planning(self):
        bodies = []
        prompt = "blues chaud et dynamique"
        responses = [
            fallback_tone_intent(prompt).model_dump_json(),
            json.dumps(proposal()),
        ]

        def handler(incoming: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(incoming.content))
            return httpx.Response(200, json={"message": {"content": responses.pop(0)}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                return await OllamaClient(self.config(), http_client).propose(request(prompt))

        value = asyncio.run(run())
        self.assertEqual(len(bodies), 2)
        first_payload = json.loads(bodies[0]["messages"][-1]["content"])
        second_payload = json.loads(bodies[1]["messages"][-1]["content"])
        self.assertNotIn("plugins", first_payload)
        self.assertIn("tone_intent", second_payload)
        self.assertEqual(
            [item["plugin_id"] for item in second_payload["candidate_shortlist"]["plugins"]],
            [INPUT_ID],
        )
        self.assertEqual(value.request_id, "job_test")

    def test_retries_a_plan_that_invents_a_plugin(self):
        prompt = "son clair"
        answers = [
            fallback_tone_intent(prompt).model_dump_json(),
            json.dumps(proposal("plg_999999999999999999999999")),
            json.dumps(proposal()),
        ]
        calls = 0

        def handler(_incoming: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"message": {"content": answers.pop(0)}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                return await OllamaClient(self.config(), http_client).propose(request(prompt))

        value = asyncio.run(run())
        self.assertEqual(calls, 3)
        self.assertEqual(value.proposals[0].chain[0].plugin_id, INPUT_ID)

    def test_fails_after_bounded_invalid_answers(self):
        prompt = "son clair"

        def handler(_incoming: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": {"content": "not-json"}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                await OllamaClient(self.config(retries=1), http_client).propose(request(prompt))

        with self.assertRaises(RemoteServiceError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
