from __future__ import annotations

import hashlib
import asyncio
import json
import tempfile
import unittest
import zipfile
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

from pipedal_ai.catalog import CatalogService
from pipedal_ai.compiler import PresetCompiler
from pipedal_ai.config import load_pi_config
from pipedal_ai.db import Database
from pipedal_ai.degraded import DegradedProposer, _choose_asset, _find_plugin, _prompt_tags
from pipedal_ai.errors import ConfigurationError, ContractError, RemoteServiceError
from pipedal_ai.models import CatalogRef
from pipedal_ai.pi.jobs import JobManager


def descriptor(uri: str, name: str, controls: list[dict]) -> str:
    ports = [
        {"index": 0, "symbol": "in", "name": "In", "direction": "input", "kind": "audio", "datatype": "number", "minimum": None, "maximum": None, "default": None, "properties": [], "scale_points": [], "supported_events": []},
        {"index": 1, "symbol": "out", "name": "Out", "direction": "output", "kind": "audio", "datatype": "number", "minimum": None, "maximum": None, "default": None, "properties": [], "scale_points": [], "supported_events": []},
    ] + [{"index": i + 2, "name": p["symbol"], "direction": "input", "kind": "control", "properties": [], "scale_points": [], "supported_events": [], **p} for i, p in enumerate(controls)]
    return json.dumps({"uri": uri, "name": name, "author": "test", "class": "Plugin", "has_latency": False, "ports": ports})


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "catalog.db")
        self.db.initialize()
        content = b"NAM fixture\n"
        asset = self.root / "uploads/NeuralAmpModels/model.nam"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        plugins = [
            ("plg_111111111111111111111111", "http://two-play.com/plugins/toob-input_stage", "TooB Input Stage", [{"symbol": "trim", "datatype": "number", "minimum": -60, "maximum": 30, "default": 0}]),
            ("plg_222222222222222222222222", "http://two-play.com/plugins/toob-nam", "TooB Neural Amp Modeler", [{"symbol": "inputGain", "datatype": "number", "minimum": -40, "maximum": 40, "default": 0}]),
            ("plg_333333333333333333333333", "http://two-play.com/plugins/toob-volume", "TooB Volume", [{"symbol": "vol", "datatype": "number", "minimum": -60, "maximum": 30, "default": 0}]),
        ]
        with self.db.transaction() as c:
            c.execute("INSERT INTO catalog_revisions VALUES(1,?,?,?,?,?,?,?,?,?,?)", ("a"*64,"pipedal-ai.catalog-source/1.0.0","pipedal-ai.inventory/1.0.0","b"*64,"test","2026-01-01T00:00:00Z","2026-01-01T00:00:00Z",3,1,0))
            for pid, uri, name, controls in plugins:
                value = descriptor(uri, name, controls)
                c.execute("INSERT INTO catalog_plugins VALUES(1,?,?,?,?,?,?,?,?)", (pid,uri,name,"Plugin","test",0,"c"*64,value))
            c.execute("INSERT INTO catalog_assets VALUES(1,?,?,?,?,?,?)", ("ast_111111111111111111111111","nam","NeuralAmpModels/model.nam",".nam",len(content),digest))
            c.execute("INSERT INTO catalog_state VALUES(1,1)")
        self.catalog = CatalogService(self.db)

    def tearDown(self): self.temp.cleanup()

    def test_capabilities_hide_local_paths(self):
        encoded = json.dumps(self.catalog.capabilities())
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("relative_path", encoded)

    def test_catalog_exposes_only_explicit_asset_metadata(self):
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO asset_metadata VALUES(?,?,?,?,?)",
                (1, "ast_111111111111111111111111", "tone3000",
                 json.dumps({"title": "Warm Tweed", "gear": "amp"}), "2026-01-01T00:00:00Z"),
            )
        asset = self.catalog.capabilities()["assets"][0]
        self.assertEqual(asset["metadata"]["tone3000"]["title"], "Warm Tweed")
        self.assertNotIn("relative_path", asset)

    def test_degraded_three_variants_validate(self):
        value = DegradedProposer().propose("req", "warm blues crunch", self.catalog.capabilities())
        self.assertEqual([p.variant for p in value.proposals], ["conservative", "balanced", "bold"])
        self.catalog.validate_proposal_set(value)

    def test_french_prompt_is_normalized(self):
        tags = _prompt_tags("Son clair, chaud et légèrement saturé, avec une pièce légère")
        self.assertTrue({"clean", "warm", "crunch", "space"}.issubset(tags))
        self.assertNotIn("high_gain", tags)

    def test_plugin_priority_does_not_depend_on_catalog_order(self):
        capabilities = {
            "plugins": [
                {"plugin_id": "club", "name": "GxClubDrive", "uri": "clubdrive"},
                {"plugin_id": "ts", "name": "GxTubeScreamer", "uri": "gxts9"},
            ]
        }
        selected = _find_plugin(capabilities, "gxtubescreamer", "clubdrive")
        self.assertEqual(selected["plugin_id"], "ts")

    def test_french_tone_selects_matching_nam(self):
        capabilities = {
            "assets": [
                {"asset_id": "clean", "kind": "nam", "display_name": "Orange AD30 Cleanest"},
                {"asset_id": "crunch", "kind": "nam", "display_name": "Fender Tweed Deluxe Crunch Warm"},
                {"asset_id": "metal", "kind": "nam", "display_name": "EVH 5150 Metal Lead"},
            ]
        }
        selected = _choose_asset(capabilities, "nam", "blues chaud avec un crunch léger")
        self.assertEqual(selected["asset_id"], "crunch")

    def test_compiler_embeds_verified_media_and_pipedal_shape(self):
        spec = DegradedProposer().propose("req", "clean", self.catalog.capabilities()).proposals[1]
        compiler = PresetCompiler(self.catalog, self.root / "uploads", 1_000_000)
        result = compiler.compile(spec, self.root / "artifacts")
        second = compiler.compile(spec, self.root / "artifacts-2")
        self.assertEqual(result["sha256"], second["sha256"])
        self.assertRegex(result["sha256"], r"^[a-f0-9]{64}$")
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual(set(archive.namelist()), {"bankFile.json", "pluginsUsed.json", "media/NeuralAmpModels/model.nam"})
            bank = json.loads(archive.read("bankFile.json"))
            item = next(x for x in bank["presets"][0]["preset"]["items"] if x["uri"].endswith("toob-nam"))
            self.assertTrue(item["lv2State"][0])
            state_entry = item["lv2State"][1]["http://two-play.com/plugins/toob-nam#modelFile"]
            self.assertEqual(list(state_entry), ["flags", "atomType", "value"])
            self.assertEqual(state_entry["value"], "NeuralAmpModels/model.nam")

    def test_catalog_mismatch_is_rejected(self):
        value = DegradedProposer().propose("req", "clean", self.catalog.capabilities())
        value.proposals[0].catalog.sha256 = "d" * 64
        with self.assertRaises(Exception): self.catalog.validate_proposal_set(value)

    def test_capability_snapshot_uses_the_job_catalog_not_the_new_active_one(self):
        frozen = self.catalog.active_ref()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog_revisions VALUES(2,?,?,?,?,?,?,?,?,?,?)",
                ("d" * 64, "pipedal-ai.catalog-source/1.0.0", "pipedal-ai.inventory/1.0.0",
                 "e" * 64, "test", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", 0, 0, 0),
            )
            connection.execute("UPDATE catalog_state SET active_revision=2 WHERE singleton=1")
        snapshot = self.catalog.capabilities(frozen)
        self.assertEqual(snapshot["catalog"], frozen.model_dump(mode="json"))
        self.assertEqual(len(snapshot["plugins"]), 3)

    def test_pi_rejects_a_response_for_another_job(self):
        proposal = DegradedProposer().propose("wrong_job", "clean", self.catalog.capabilities())
        with self.assertRaises(ContractError):
            JobManager._validate_correlated(proposal, "expected_job", self.catalog.active_ref())

    def test_non_loopback_pi_requires_api_key(self):
        config = self.root / "pi.toml"
        config.write_text(
            "[server]\nhost='0.0.0.0'\napi_key_env='TEST_MISSING_PI_KEY'\n"
            "[rtx]\nenabled=false\n[pipedal]\n[storage]\n[policy]\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {}, clear=False):
            with self.assertRaises(ConfigurationError):
                load_pi_config(config)

    def run_job(self, rtx_answer=None, rtx_error=None):
        compiler = PresetCompiler(self.catalog, self.root / "uploads", 1_000_000)
        rtx = Mock()
        rtx.propose = AsyncMock(return_value=rtx_answer, side_effect=rtx_error)
        from pipedal_ai.models import TextPresetJobRequest

        async def run():
            manager = JobManager(self.db, self.catalog, compiler, rtx, Mock(), Mock(), self.root / "artifacts")
            job_id = manager.create(TextPresetJobRequest(prompt="son clean"))
            await asyncio.gather(*manager._tasks)
            return manager.get(job_id)

        return asyncio.run(run())

    def test_completed_degraded_job_preserves_upstream_failure_reason(self):
        value = self.run_job(rtx_error=RemoteServiceError("Ollama étape=plan HTTP 400 : invalid option"))
        self.assertEqual(value.status, "completed")
        self.assertEqual(value.source, "degraded")
        self.assertIn("invalid option", value.fallback_reason)
        self.assertIsNone(value.error)
        self.assertEqual(len(value.artifacts), 3)

    def test_pi_validation_fallback_is_visible(self):
        wrong = DegradedProposer().propose("wrong_job", "clean", self.catalog.capabilities())
        value = self.run_job(rtx_answer=wrong)
        self.assertEqual(value.status, "completed")
        self.assertEqual(value.source, "degraded")
        self.assertIn("Validation sur le Pi", value.fallback_reason)
        self.assertIn("travail demandé", value.fallback_reason)

    def test_sqlite_v3_migration_preserves_existing_jobs_and_is_idempotent(self):
        job = self.run_job(rtx_error=RemoteServiceError("RTX indisponible"))
        with self.db.connect() as connection:
            connection.execute("ALTER TABLE jobs DROP COLUMN fallback_reason")
            connection.execute("PRAGMA user_version=3")
        self.db.initialize()
        self.db.initialize()
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["prompt"], job.prompt)
            self.assertIsNone(row["fallback_reason"])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 3)

    def test_http_job_creation_runs_on_event_loop_and_finishes_with_rtx_source(self):
        import httpx
        from pipedal_ai.pi.app import create_app

        config_path = self.root / "pi.toml"
        config_path.write_text(
            "[server]\nhost='127.0.0.1'\n[rtx]\nenabled=false\n[storage]\n" +
            f"database={json.dumps(str(self.db.path))}\n" +
            f"artifact_root={json.dumps(str(self.root / 'http-artifacts'))}\n" +
            f"upload_root={json.dumps(str(self.root / 'uploads'))}\n",
            encoding="utf-8",
        )
        config = load_pi_config(config_path)
        app = create_app(config)
        app.state.jobs.guard = Mock()

        async def propose(value):
            return DegradedProposer().propose(value.request_id, value.prompt, value.capabilities)

        app.state.jobs.rtx.propose = AsyncMock(side_effect=propose)
        headers = {"X-PiPedal-AI-Key": config.server.api_key}
        async def run():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                created = await client.post("/api/v1/jobs/text", json={"prompt": "son clean"}, headers=headers)
                self.assertEqual(created.status_code, 202)
                await asyncio.gather(*app.state.jobs._tasks)
                return (await client.get(created.json()["status_url"], headers=headers)).json()

        value = asyncio.run(run())
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["source"], "rtx")
        self.assertIsNone(value["fallback_reason"])
        self.assertEqual(len(value["artifacts"]), 3)

    def test_diagnose_cli_compiles_three_presets_without_import(self):
        import io
        from contextlib import redirect_stdout
        from pipedal_ai.cli import main

        config = Mock()
        compiler = PresetCompiler(self.catalog, self.root / "uploads", 1_000_000)
        output = self.root / "diagnostic-artifacts"

        async def propose(value):
            return DegradedProposer().propose(value.request_id, value.prompt, value.capabilities)

        result = io.StringIO()
        with patch("pipedal_ai.cli._services", return_value=(config, self.db, self.catalog, compiler)), \
             patch("pipedal_ai.cli.RTXClient") as client, \
             patch("sys.argv", ["pipedal-ai", "diagnose-rtx", "--prompt", "son clean", "--output", str(output)]), \
             redirect_stdout(result):
            client.return_value.propose = AsyncMock(side_effect=propose)
            main()
        value = json.loads(result.getvalue())
        self.assertEqual(value["source"], "rtx")
        self.assertFalse(value["imported"])
        self.assertEqual(len(value["artifacts"]), 3)
        self.assertEqual(len(list(output.glob("*.piPreset"))), 3)

    def test_diagnose_cli_failure_does_not_fall_back_or_compile(self):
        from pipedal_ai.cli import main

        output = self.root / "failed-diagnostic"
        with patch("pipedal_ai.cli._services", return_value=(Mock(), self.db, self.catalog, Mock())), \
             patch("pipedal_ai.cli.RTXClient") as client, \
             patch("sys.argv", ["pipedal-ai", "diagnose-rtx", "--prompt", "son clean", "--output", str(output)]):
            client.return_value.propose = AsyncMock(side_effect=RemoteServiceError("Ollama plan refusé"))
            with self.assertRaisesRegex(SystemExit, "aucun repli local.*Ollama plan refusé"):
                main()
        self.assertFalse(output.exists())


if __name__ == "__main__": unittest.main()
