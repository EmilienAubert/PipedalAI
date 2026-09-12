from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from pipedal_ai.catalog import CatalogService
from pipedal_ai.config import OllamaConfig
from pipedal_ai.db import Database
from pipedal_ai.intent import fallback_tone_intent
from pipedal_ai.models import RTXProposalRequest
from pipedal_ai.rtx.ollama import OllamaClient


ROOT = Path(__file__).resolve().parents[1]


class CurrentCatalogIntegrationTests(unittest.TestCase):
    def test_real_inventory_import_and_constrained_nam_plan_with_mocked_ollama(self):
        """Real catalogue fixture and validation, not a real model or audio render."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "catalog.db")
            database.initialize()
            imported = subprocess.run(
                [sys.executable, str(ROOT / "tools/phase0/pipedal_ai_catalog.py"), "import",
                 "--inventory", str(ROOT / "examples/current-pi/pipedal-inventory-v2.json"),
                 "--database", str(database.path)], capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            catalog = CatalogService(database)
            capabilities = catalog.capabilities()
            self.assertGreater(len(capabilities["plugins"]), 0)
            self.assertGreater(len(capabilities["assets"]), 0)
            prompt = "Son blues chaud, léger crunch dynamique"
            calls = 0

            def handler(incoming):
                nonlocal calls
                calls += 1
                body = json.loads(incoming.content)
                if calls == 1:
                    answer = fallback_tone_intent(prompt).model_dump(mode="json")
                else:
                    data = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
                    shortlist = data["candidate_shortlist"]
                    plugin = next(p for p in shortlist["plugins"] if p["resource_roles"] == ["nam_model"])
                    asset = next(a for a in shortlist["assets"] if a["resource_role"] == "nam_model")
                    answer = {
                        "schema_version": "pipedal-ai.plan-draft/1.0.0", "request_id": data["request_id"],
                        "catalog": data["catalog"],
                        "variants": [{"variant": variant, "description": "Test du contrat avec NAM installé",
                                      "chain": [{"instance_id": "amp", "plugin_id": plugin["plugin_id"],
                                                 "resources": [{"role": "nam_model", "asset_id": asset["asset_id"]}]}]}
                                     for variant in ("conservative", "balanced", "bold")],
                    }
                return httpx.Response(200, json={"done": True, "done_reason": "stop",
                                               "message": {"content": json.dumps(answer)}})

            async def run():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    return await OllamaClient(OllamaConfig("http://ollama.test", "mock", 10, 0), client).propose(
                        RTXProposalRequest(schema_version="pipedal-ai.rtx-request/1.0.0",
                                           request_id="catalog_integration", prompt=prompt, capabilities=capabilities))

            proposal = asyncio.run(run())
            catalog.validate_proposal_set(proposal)
            self.assertEqual(calls, 2)
            self.assertEqual(proposal.catalog, catalog.active_ref())
