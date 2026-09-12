from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from pipedal_ai.config import RTXClientConfig
from pipedal_ai.errors import RemoteServiceError
from pipedal_ai.pi.rtx_client import RTXClient
from test_ollama_pipeline import request


class RTXClientErrorTests(unittest.TestCase):
    def test_http_502_preserves_ollama_detail_for_pi_fallback(self):
        config = RTXClientConfig("http://rtx.test", 10, "test-token", None, None, None, True)

        def handler(incoming):
            self.assertEqual(incoming.headers["Authorization"], "Bearer test-token")
            return httpx.Response(502, json={"detail": "Ollama étape=plan : variants absent"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run():
            with patch("pipedal_ai.pi.rtx_client.httpx.AsyncClient", return_value=client):
                await RTXClient(config).propose(request())

        with self.assertRaisesRegex(RemoteServiceError, "RTX HTTP 502.*variants absent"):
            asyncio.run(run())
