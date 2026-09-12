from __future__ import annotations

import asyncio
import json
import unittest
import tempfile
from dataclasses import replace
from pathlib import Path

import httpx

from pipedal_ai.config import OllamaConfig
from pipedal_ai.degraded import DegradedProposer
from pipedal_ai.errors import RemoteServiceError
from pipedal_ai.intent import fallback_tone_intent
from pipedal_ai.models import PlanDraft, RTXProposalRequest
from pipedal_ai.rtx.ollama import OllamaClient
from pipedal_ai.rtx.ollama_schema import generation_schema


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


def draft(plugin_id: str = INPUT_ID) -> dict:
    value = proposal(plugin_id)
    return {
        "schema_version": "pipedal-ai.plan-draft/1.0.0",
        "request_id": value["request_id"], "catalog": value["catalog"],
        "variants": [{key: preset[key] for key in ("variant", "description", "chain")}
                     for preset in value["proposals"]],
    }


def input_payload(body: dict) -> dict:
    message = next(item for item in body["messages"] if item["role"] == "user")
    return json.loads(message["content"].split("\n", 1)[1])


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
            json.dumps(draft()),
        ]

        def handler(incoming: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(incoming.content))
            return httpx.Response(200, json={"message": {"content": responses.pop(0)}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                return await OllamaClient(self.config(), http_client).propose(request(prompt))

        value = asyncio.run(run())
        self.assertEqual(len(bodies), 2)
        first_payload = input_payload(bodies[0])
        second_payload = input_payload(bodies[1])
        self.assertNotIn("plugins", first_payload)
        self.assertIn("tone_intent", second_payload)
        self.assertEqual(
            [item["plugin_id"] for item in second_payload["candidate_shortlist"]["plugins"]],
            [INPUT_ID],
        )
        self.assertEqual(value.request_id, "job_test")
        self.assertNotIn("selection", second_payload["candidate_shortlist"])
        self.assertFalse(bodies[0]["think"])
        self.assertEqual(bodies[1]["format"]["required"], ["schema_version", "request_id", "catalog", "variants"])
        self.assertEqual(bodies[1]["format"]["$defs"]["chain"]["maxItems"], 8)
        self.assertIn('"variants"', bodies[1]["messages"][0]["content"])
        self.assertEqual(value.schema_version, "pipedal-ai.proposal-set/1.0.0")

    def test_retries_a_plan_that_invents_a_plugin(self):
        prompt = "son clair"
        answers = [
            fallback_tone_intent(prompt).model_dump_json(),
            json.dumps(draft("plg_999999999999999999999999")),
            json.dumps(draft()),
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

    def generate(self, answers, *, config=None):
        bodies = []

        def handler(incoming):
            bodies.append(json.loads(incoming.content))
            answer = answers.pop(0)
            return answer if isinstance(answer, httpx.Response) else httpx.Response(
                200, json={"done": True, "done_reason": "stop", "message": {"content": json.dumps(answer)}})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await OllamaClient(config or self.config(retries=0), client).propose(request())

        return asyncio.run(run()), bodies

    def test_catalog_summary_and_tool_results_are_not_a_plan(self):
        intent = fallback_tone_intent(request().prompt).model_dump(mode="json")
        invalid = [
            {"plugin_count": 24, "selected_assets": [], "message": "Selected reverb assets"},
            {"type": "tool_result", "is_error": False,
             "content": [{"type": "text", "text": json.dumps(draft())}]},
            {"plan_draft": draft()},
            proposal(),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(RemoteServiceError, "étape=plan"):
                self.generate([intent, value])

    def test_other_request_or_catalog_is_rejected_without_relabelling(self):
        intent = fallback_tone_intent(request().prompt).model_dump(mode="json")
        for field in ("request_id", "catalog"):
            value = draft()
            value[field] = "wrong_job" if field == "request_id" else {"revision": 9, "sha256": "f" * 64}
            with self.subTest(field=field), self.assertRaisesRegex(RemoteServiceError, "modifié"):
                self.generate([intent, value])

    def test_retry_contains_rejected_answer_and_actual_validation_reason(self):
        intent = fallback_tone_intent(request().prompt).model_dump(mode="json")
        bad = {"plugin_count": 24, "selected_assets": []}
        _, bodies = self.generate([intent, bad, draft()], config=self.config(retries=1))
        self.assertEqual(bodies[-1]["messages"][-2]["role"], "assistant")
        self.assertEqual(json.loads(bodies[-1]["messages"][-2]["content"]), bad)
        self.assertIn("variants", bodies[-1]["messages"][-1]["content"])

    def test_http_400_keeps_actual_ollama_error_and_does_not_relax_format(self):
        bodies = []

        def handler(incoming):
            bodies.append(json.loads(incoming.content))
            return httpx.Response(400, json={"error": "invalid option num_ctx"})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await OllamaClient(self.config(retries=3), client).propose(request())

        with self.assertRaisesRegex(RemoteServiceError, "HTTP 400.*invalid option num_ctx"):
            asyncio.run(run())
        self.assertEqual(len(bodies), 1)
        self.assertIsInstance(bodies[0]["format"], dict)

    def test_empty_thinking_only_and_truncated_responses_have_clear_errors(self):
        envelopes = [
            ({"done_reason": "stop", "message": {"content": "", "thinking": "reasoning"}}, "vide"),
            ({"done_reason": "length", "message": {"content": "{}"}}, "tronquée"),
            ({"done": False, "message": {"content": "{}"}}, "incomplète"),
        ]
        for envelope, expected in envelopes:
            with self.subTest(expected=expected), self.assertRaisesRegex(RemoteServiceError, expected):
                self.generate([httpx.Response(200, json=envelope)])

    def test_json_compatibility_is_explicit_and_keeps_schema_in_prompt(self):
        config = replace(self.config(retries=0), output_format="json", think="low")
        intent = fallback_tone_intent(request().prompt).model_dump(mode="json")
        _, bodies = self.generate([intent, draft()], config=config)
        self.assertTrue(all(body["format"] == "json" for body in bodies))
        self.assertEqual(bodies[1]["think"], "low")
        self.assertIn('"variants"', bodies[1]["messages"][0]["content"])

    def test_diagnostics_capture_exchange_without_authorization_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config(retries=0), diagnostics_directory=Path(directory))
            intent = fallback_tone_intent(request().prompt).model_dump(mode="json")
            self.generate([intent, draft()], config=config)
            records = [json.loads(p.read_text(encoding="utf-8")) for p in Path(directory).glob("*.json")]
            self.assertEqual({record["stage"] for record in records}, {"intent", "plan"})
            for record in records:
                self.assertIsNone(record["error"])
                self.assertIn("message", json.loads(record["response_raw"]))
                self.assertNotIn("Authorization", record["request_json"])

    def test_inline_schema_preserves_property_names_and_constraints(self):
        schema = generation_schema(PlanDraft)
        variant = schema["properties"]["variants"]["items"]
        self.assertIn("description", variant["properties"])
        self.assertIn("description", variant["required"])
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["variants"]["minItems"], 3)
        self.assertEqual(variant["properties"]["chain"]["items"]["properties"]["plugin_id"]["pattern"],
                         r"^plg_[a-f0-9]{24}$")


if __name__ == "__main__":
    unittest.main()
