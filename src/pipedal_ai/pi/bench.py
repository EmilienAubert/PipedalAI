"""Pi-owned bench orchestration, bounded search, persistence and preview delivery."""
from __future__ import annotations

import json
from pathlib import Path

from ..asset_metadata import capture_info
from ..audio_io import atomic_json, audio_levels, digest_file, preview_audio, read_audio, safe_path, write_pcm
from ..intent import ToneIntent, fallback_tone_intent
from ..knowledge import TOOB
from ..models import ChainStep, PresetSpec, ProposalSet
from ..errors import ContractError
from ..rtx.audio_analysis import multilevel_summary, score_features
from .jobs import now


def optimization_grid(spec):
    """Small interpretable neighbourhood, never arbitrary plugin parameters."""
    result = [spec.model_copy(deep=True)]
    for role, symbol, delta in (("amp", "inputGain", -1.5), ("amp", "inputGain", 1.5),
                                 ("eq", "hfLevel", -1.5), ("eq", "hfLevel", 1.5)):
        candidate = spec.model_copy(deep=True)
        step = next((s for s in candidate.chain if s.instance_id == role and symbol in s.parameters), None)
        if step is not None:
            step.parameters[symbol] = round(float(step.parameters[symbol]) + delta, 4)
            result.append(candidate)
    return result


class BenchService:
    def __init__(self, database, catalog, compiler, renderer, rtx, config):
        self.database, self.catalog, self.compiler = database, catalog, compiler
        self.renderer, self.rtx, self.config = renderer, rtx, config

    def di_file(self, set_id, di_id=None):
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM di_sets WHERE set_id=?", (set_id,)).fetchone()
        if row is None:
            raise ValueError("Jeu de DI inconnu : utiliser di-import")
        payload = json.loads(row["manifest_json"])
        records = payload["audio"]
        if di_id is None:
            di_id = next((f["di_id"] for f in payload["manifest"]["files"] if f["purpose"] == "dynamics"), records[0]["di_id"])
        record = next((r for r in records if r["di_id"] == di_id), None)
        if record is None:
            raise ValueError("DI absente du jeu")
        path = safe_path(Path(row["root"]), di_id + ".wav")
        if digest_file(path) != record["sha256"]:
            raise ValueError("DI modifiée après ingestion")
        return path, payload

    def start_session(self, set_id, job_id=None):
        active = self.catalog.active_ref()
        session_id = self.database.new_id("bench")
        self.config.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = safe_path(self.config.output_root, session_id, exists=False)
        folder.mkdir(mode=0o700)
        with self.database.transaction() as c:
            c.execute("INSERT INTO bench_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                      (session_id, job_id, set_id, active.revision, active.sha256, "running", None, None, now()))
        return session_id, folder, active

    def finish(self, session_id, report=None, error=None):
        with self.database.transaction() as c:
            c.execute("UPDATE bench_sessions SET status=?,report_json=?,error=? WHERE session_id=?",
                      ("failed" if error else "completed", self.database.json(report) if report else None,
                       str(error)[:2000] if error else None, session_id))

    async def evaluate_job(self, job_id, set_id, *, di_id=None, maintenance_confirmed=False, optimize=True, session=None):
        if not self.config.enabled or not maintenance_confirmed:
            raise ContractError("Activer [bench] et confirmer une séance hors live")
        with self.database.connect() as c:
            job = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job or job["status"] != "completed" or not job["proposal_json"]:
            raise ValueError("Travail texte terminé requis")
        proposal = ProposalSet.model_validate_json(job["proposal_json"])
        self.catalog.validate_proposal_set(proposal)
        di_path, manifest = self.di_file(set_id, di_id)
        intent = ToneIntent.model_validate(proposal.decision_report["tone_intent"]) if proposal.decision_report else fallback_tone_intent(job["prompt"])
        session_id, folder, frozen = session or self.start_session(set_id, job_id)
        report = {"session_id": session_id, "job_id": job_id, "set_id": set_id,
                  "catalog": frozen.model_dump(mode="json"), "candidates": [], "warnings": [], "optimized_artifacts": []}
        try:
            count = 0
            # Evaluate original A/B/C first, then their bounded neighbourhood.
            queue = [(spec, 0) for spec in proposal.proposals]
            if optimize:
                queue += [(candidate, number) for spec in proposal.proposals
                          for number, candidate in enumerate(optimization_grid(spec)[1:], 1)]
            seen = set()
            for spec, number in queue:
                if count >= self.config.max_renders:
                    report["warnings"].append("Budget de rendus atteint")
                    break
                content = self.database.json([s.model_dump(mode="json") for s in spec.chain])
                if (spec.variant, content) in seen:
                    continue
                seen.add((spec.variant, content))
                try:
                    self.catalog.validate_preset(spec)
                except ContractError:
                    if number == 0:
                        raise
                    report["warnings"].append("Voisin hors limites ignoré")
                    continue
                if self.catalog.active_ref() != frozen:
                    raise ContractError("Catalogue modifié pendant l'optimisation")
                identifier = self.database.new_id("cand")
                rendered = folder / (identifier + ".wav")
                runtime = await self.renderer.render(spec, di_path, rendered, maintenance_confirmed=True)
                analysis = await self.rtx.analyze_pair(di_path, rendered, frozen, identifier)
                # Safety remains an independent decision of the Pi.
                local_audio, _ = read_audio(rendered)
                local_levels = audio_levels(local_audio)
                score = score_features(analysis["features"], intent)
                preview = folder / (identifier + "-preview.wav")
                match = preview_audio(rendered, preview)
                safe = (local_levels["clipped_ratio"] == 0
                        and local_levels["peak_dbfs"] <= -1.0
                        and analysis["alignment_confidence"] >= 0.3)
                metrics = {"analysis": analysis, "runtime": runtime, "preview": match,
                           "pi_levels": local_levels, "preview_sha256": digest_file(preview),
                           "original": number == 0, "eligible_for_export": safe}
                with self.database.transaction() as c:
                    c.execute("INSERT INTO bench_candidates VALUES(?,?,?,?,?,?,?,?,?)",
                              (identifier, session_id, self.database.json(spec.model_dump(mode="json")),
                               str(rendered.absolute()), digest_file(rendered), str(preview.absolute()),
                               self.database.json(metrics), score["total"], now()))
                report["candidates"].append({"candidate_id": identifier, "variant": spec.variant,
                    "score": score, "eligible_for_export": safe, "original": number == 0,
                    "features": analysis["features"], "warnings": analysis["warnings"]})
                count += 1
            # Export only exactly rendered eligible specs, not unverified post-hoc tweaks.
            for variant in ("conservative", "balanced", "bold"):
                candidates = [c for c in report["candidates"] if c["variant"] == variant and c["eligible_for_export"]]
                if not candidates:
                    report["warnings"].append(f"{variant}: aucun rendu éligible à l'export")
                    continue
                chosen = min(candidates, key=lambda c: c["score"]["total"])
                with self.database.connect() as c:
                    row = c.execute("SELECT spec_json FROM bench_candidates WHERE candidate_id=?", (chosen["candidate_id"],)).fetchone()
                spec = PresetSpec.model_validate_json(row["spec_json"])
                spec.name = (spec.name[:62] + " - measured")[:80]
                artifact = self.compiler.compile(spec, folder / "presets")
                chosen["selected"] = True
                report["optimized_artifacts"].append(artifact)
            chosen = [c for c in report["candidates"] if c.get("selected")]
            for i, a in enumerate(chosen):
                for b in chosen[i + 1:]:
                    keys = [k for k in a["features"] if a["features"][k] is not None and b["features"].get(k) is not None]
                    distance = sum((a["features"][k] - b["features"][k]) ** 2 for k in keys) ** 0.5
                    if distance < 0.03:
                        report["warnings"].append(f"{a['variant']}/{b['variant']}: profils mesurés proches; comparer à l'oreille")
            atomic_json(folder / "report.json", report)
            self.finish(session_id, report)
            return report
        except BaseException as exc:
            self.finish(session_id, report, str(exc) or type(exc).__name__)
            raise

    async def characterize(self, set_id, *, nam_limit=3, cab_ir_id=None, di_id=None, maintenance_confirmed=False):
        if not self.config.enabled or not maintenance_confirmed:
            raise ContractError("Caractérisation réservée au banc hors live explicitement activé")
        di_path, manifest = self.di_file(set_id, di_id)
        capabilities = self.catalog.capabilities()
        host = next((p for p in capabilities["plugins"] if p["uri"] == TOOB + "nam"), None)
        cab_host = next((p for p in capabilities["plugins"] if p["uri"] == TOOB + "cab-ir"), None)
        if not host:
            raise ValueError("TooB NAM absent")
        if cab_ir_id:
            cab_asset = next((a for a in capabilities["assets"] if a["asset_id"] == cab_ir_id and a["kind"] == "cab_ir"), None)
            if not cab_host or not cab_asset:
                raise ValueError("IR ou chargeur de cabinet absent")
        session_id, folder, frozen = self.start_session(set_id)
        report = {"session_id": session_id, "catalog": frozen.model_dump(mode="json"), "profiles": [], "warnings": []}
        data, rate = read_audio(di_path, require_di=True)
        count = 0
        try:
            nam_assets = [a for a in capabilities["assets"] if a["kind"] == "nam"][:max(1, min(nam_limit, 10000))]
            for asset in nam_assets:
                if count + 3 > self.config.max_renders:
                    report["warnings"].append("Budget atteint : relancer la même commande reprend avec le cache")
                    break
                capture = capture_info(asset)["capture_type"]
                if capture == "amp" and not cab_ir_id:
                    report["warnings"].append(f"{asset['asset_id']}: amp-only, fournir --cab-ir")
                    continue
                chain = [ChainStep(instance_id="amp", plugin_id=host["plugin_id"], parameters={"inputGain": 0.0},
                                   resources=[{"role": "nam_model", "asset_id": asset["asset_id"]}])]
                associated_ir = cab_ir_id if capture == "amp" else None
                if associated_ir:
                    chain.append(ChainStep(instance_id="cabinet", plugin_id=cab_host["plugin_id"],
                        parameters={"direct_mix": -40.0, "reverb_mix": 0.0, "reverb_mix2": -40.0, "reverb_mix3": -40.0},
                        resources=[{"role": "cab_ir", "asset_id": associated_ir}]))
                from .renderer import RENDERER_VERSION
                from ..rtx.audio_analysis import ANALYSIS_VERSION
                parameters = {c["symbol"]: c["default"] for c in host["controls"] if c["default"] is not None}
                parameters.update(chain[0].parameters)
                if self.config.nam_input_calibration_dbu is not None:
                    chain[0].parameters.update(inputCalibrationMode=1, calibration=self.config.nam_input_calibration_dbu)
                    parameters.update(chain[0].parameters)
                context = dict(source_di_sha256=digest_file(di_path), di_set_id=set_id, sample_rate_hz=48000,
                    nam_sha256=asset["sha256"], associated_cab_ir_id=associated_ir,
                    associated_cab_ir_sha256=cab_asset["sha256"] if associated_ir else None,
                    renderer_version=RENDERER_VERSION, analysis_version=ANALYSIS_VERSION, plugin_descriptor_sha256=host["descriptor_sha256"],
                    cab_plugin_descriptor_sha256=cab_host["descriptor_sha256"] if associated_ir else None,
                    parameters=parameters, guitar_manifest=manifest["manifest"]["files"])
                import hashlib
                context_id = hashlib.sha256(self.database.json(context).encode()).hexdigest()
                with self.database.connect() as c:
                    old = c.execute("SELECT metadata_json FROM asset_metadata WHERE revision=? AND asset_id=? AND source='characterization'", (frozen.revision, asset["asset_id"])).fetchone()
                if old and context_id in json.loads(old[0]).get("profiles", {}):
                    report["profiles"].append({"asset_id": asset["asset_id"], "context_id": context_id, "cached": True})
                    continue
                spec = PresetSpec(schema_version="pipedal-ai.preset-spec/1.0.0", catalog=frozen,
                    variant="balanced", name="Characterization " + asset["display_name"][:55], description="Banc de mesure", chain=chain)
                measurements = []
                for gain in (-6.0, 0.0, 6.0):
                    scaled = data * 10 ** (gain / 20)
                    if audio_levels(scaled)["peak_dbfs"] > -1:
                        report["warnings"].append(f"{asset['asset_id']}: niveau DI +6 dB trop fort, mesure omise")
                        continue
                    identifier = self.database.new_id("measure")
                    incoming, outgoing = folder / (identifier + "-di.wav"), folder / (identifier + "-render.wav")
                    write_pcm(incoming, scaled, rate)
                    runtime = await self.renderer.render(spec, incoming, outgoing, maintenance_confirmed=True)
                    rendered_audio, _ = read_audio(outgoing)
                    measured_levels = audio_levels(rendered_audio)
                    if measured_levels["clipped_ratio"] or measured_levels["peak_dbfs"] > -1:
                        report["warnings"].append(f"{asset['asset_id']}: rendu {gain:+g} dB trop fort, exclu du profil")
                        count += 1
                        continue
                    analysis = await self.rtx.analyze_pair(incoming, outgoing, frozen, identifier)
                    if analysis["alignment_confidence"] < 0.3:
                        report["warnings"].append(f"{asset['asset_id']}: correspondance DI/rendu insuffisante")
                        count += 1
                        continue
                    measurements.append({"input_gain_db": gain, "analysis": analysis, "runtime": runtime})
                    count += 1
                if len(measurements) < 2:
                    report["warnings"].append(f"{asset['asset_id']}: moins de deux mesures sûres, profil non inscrit")
                    continue
                summary = multilevel_summary(measurements)
                summary["context"] = context
                with self.database.transaction() as c:
                    if self.catalog.active_ref() != frozen:
                        raise ContractError("Catalogue modifié avant inscription des mesures")
                    existing = c.execute("SELECT metadata_json FROM asset_metadata WHERE revision=? AND asset_id=? AND source='characterization'",
                                         (frozen.revision, asset["asset_id"])).fetchone()
                    profiles = json.loads(existing[0]) if existing else {"profiles": {}}
                    profiles["profiles"][context_id] = summary
                    profiles["profiles"] = dict(list(profiles["profiles"].items())[-16:])
                    c.execute("""INSERT INTO asset_metadata VALUES(?,?,?,?,?) ON CONFLICT(revision,asset_id,source)
                        DO UPDATE SET metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                        (frozen.revision, asset["asset_id"], "characterization", self.database.json(profiles), now()))
                report["profiles"].append({"asset_id": asset["asset_id"], "context_id": context_id, "features": summary["features"]})
            atomic_json(folder / "report.json", report)
            self.finish(session_id, report)
            return report
        except BaseException as exc:
            self.finish(session_id, report, str(exc) or type(exc).__name__)
            raise

    def sessions(self):
        with self.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT session_id,job_id,set_id,status,error,created_at FROM bench_sessions ORDER BY created_at DESC LIMIT 30")]

    def session(self, session_id):
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM bench_sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        return {**dict(row), "report": json.loads(row["report_json"]) if row["report_json"] else None}

    def preview(self, candidate_id):
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM bench_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if not row:
            raise KeyError(candidate_id)
        render = Path(row["render_path"])
        root = self.config.output_root.resolve(strict=True)
        path = Path(row["preview_path"])
        safe_path(root, path.absolute().relative_to(root).as_posix())
        safe_path(root, render.absolute().relative_to(root).as_posix())
        if digest_file(render) != row["render_sha256"]:
            raise ValueError("Rendu modifié")
        if digest_file(path) != json.loads(row["metrics_json"])["preview_sha256"]:
            raise ValueError("Préécoute modifiée")
        return path

    def artifact(self, session_id, variant):
        session = self.session(session_id)
        if session["status"] != "completed":
            raise ValueError("Séance non terminée")
        if self.catalog.active_ref().model_dump() != {"revision": session["catalog_revision"], "sha256": session["catalog_sha256"]}:
            raise ContractError("Catalogue modifié depuis la séance")
        record = next((a for a in session["report"].get("optimized_artifacts", []) if a["variant"] == variant), None)
        if not record:
            raise KeyError(variant)
        root = self.config.output_root.resolve(strict=True)
        path = safe_path(root, Path(record["path"]).absolute().relative_to(root).as_posix())
        if digest_file(path) != record["sha256"]:
            raise ValueError("Preset mesuré modifié")
        self.compiler.validate_archive(path)
        return path
