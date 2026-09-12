from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from .db import Database
from .errors import CatalogError, ContractError
from .models import CatalogRef, ParameterValue, PresetSpec, ProposalSet


CAPABILITY_SCHEMA_VERSION = "pipedal-ai.catalog-capabilities/1.0.0"
RENDER_ONLY_URIS = {
    "http://two-play.com/plugins/toob-player",
    "http://two-play.com/plugins/toob-record-mono",
    "http://two-play.com/plugins/toob-record-stereo",
}
RESOURCE_KIND_BY_ROLE = {
    "nam_model": "nam",
    "cab_ir": "cab_ir",
    "reverb_ir": "reverb_ir",
}
PLUGIN_RESOURCE_ROLES = {
    "http://two-play.com/plugins/toob-nam": {"nam_model"},
    "http://two-play.com/plugins/toob-cab-ir": {"cab_ir"},
    "http://two-play.com/plugins/toob-convolution-reverb": {"reverb_ir"},
    "http://two-play.com/plugins/toob-convolution-reverb-stereo": {"reverb_ir"},
}


def _descriptor(row) -> dict[str, Any]:
    return json.loads(row["descriptor_json"])


class CatalogService:
    def __init__(self, database: Database, max_chain_length: int = 10):
        self.database = database
        self.max_chain_length = max_chain_length

    def active_ref(self) -> CatalogRef:
        row = self.database.active_catalog()
        return CatalogRef(revision=row["revision"], sha256=row["catalog_sha256"])

    def capabilities(self, catalog: CatalogRef | None = None) -> dict[str, Any]:
        with self.database.connect() as connection:
            if catalog is None:
                revision = self.database.active_catalog(connection)
            else:
                revision = connection.execute(
                    "SELECT * FROM catalog_revisions WHERE revision=? AND catalog_sha256=?",
                    (catalog.revision, catalog.sha256),
                ).fetchone()
                if revision is None:
                    raise CatalogError("La révision de catalogue figée n'existe plus.")
            plugins = []
            for plugin in connection.execute(
                "SELECT * FROM catalog_plugins WHERE revision=? ORDER BY uri",
                (revision["revision"],),
            ):
                descriptor = _descriptor(plugin)
                controls = []
                audio_inputs = audio_outputs = 0
                for port in descriptor["ports"]:
                    if port["kind"] == "audio":
                        audio_inputs += port["direction"] == "input"
                        audio_outputs += port["direction"] == "output"
                    if port["kind"] == "control" and port["direction"] == "input":
                        controls.append(
                            {
                                "symbol": port["symbol"],
                                "name": port["name"],
                                "datatype": port["datatype"],
                                "minimum": port["minimum"],
                                "maximum": port["maximum"],
                                "default": port["default"],
                                "scale_points": port["scale_points"],
                            }
                        )
                plugins.append(
                    {
                        "plugin_id": plugin["plugin_id"],
                        "uri": plugin["uri"],
                        "name": plugin["name"],
                        "class": plugin["class"],
                        "descriptor_sha256": plugin["descriptor_sha256"],
                        "audio_inputs": audio_inputs,
                        "audio_outputs": audio_outputs,
                        "allowed_in_live_chain": plugin["uri"] not in RENDER_ONLY_URIS,
                        "resource_roles": sorted(PLUGIN_RESOURCE_ROLES.get(plugin["uri"], set())),
                        "controls": controls,
                    }
                )
                from .knowledge import plugin_knowledge
                plugins[-1]["knowledge"] = plugin_knowledge(plugins[-1])
            metadata_by_asset: dict[str, dict[str, Any]] = {}
            for metadata_row in connection.execute(
                "SELECT asset_id,source,metadata_json FROM asset_metadata WHERE revision=? ORDER BY asset_id,source",
                (revision["revision"],),
            ):
                metadata = json.loads(metadata_row["metadata_json"])
                if metadata_row["source"] == "characterization":
                    # Render journals and local paths stay on the Pi. The RTX
                    # needs compact vectors and their valid measurement context.
                    nam_host = next((p for p in plugins if p["uri"] == "http://two-play.com/plugins/toob-nam"), None)
                    profiles = {}
                    for key, profile in metadata.get("profiles", {}).items():
                        context = profile.get("context", {})
                        if not nam_host or context.get("plugin_descriptor_sha256") != nam_host["descriptor_sha256"]:
                            continue
                        safe_context = {k: context.get(k) for k in ("source_di_sha256", "di_set_id", "sample_rate_hz", "nam_sha256",
                            "associated_cab_ir_id", "associated_cab_ir_sha256", "renderer_version", "analysis_version", "plugin_descriptor_sha256", "parameters")}
                        profiles[key] = {"features": profile["features"], "context": safe_context,
                                         "level_response_slope": profile.get("level_response_slope"),
                                         "confidence": profile.get("confidence"), "limitations": profile.get("limitations")}
                    metadata = {"profiles": profiles}
                metadata_by_asset.setdefault(metadata_row["asset_id"], {})[metadata_row["source"]] = metadata
            assets = []
            for row in connection.execute(
                "SELECT * FROM catalog_assets WHERE revision=? ORDER BY asset_id",
                (revision["revision"],),
            ):
                asset = {
                    "asset_id": row["asset_id"],
                    "display_name": PurePosixPath(row["relative_path"]).stem,
                    "kind": row["kind"],
                    "extension": row["extension"],
                    "size_bytes": row["size_bytes"],
                    "sha256": row["sha256"],
                }
                if row["asset_id"] in metadata_by_asset:
                    asset["metadata"] = metadata_by_asset[row["asset_id"]]
                from .asset_metadata import capture_info
                asset.update(capture_info(asset))
                assets.append(asset)
        payload = {
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "catalog": {"revision": revision["revision"], "sha256": revision["catalog_sha256"]},
            "plugins": plugins,
            "assets": assets,
        }
        from hashlib import sha256

        capability_sha256 = sha256(Database.json(payload).encode()).hexdigest()
        return {**payload, "capability_sha256": capability_sha256}

    def validate_proposal_set(self, value: ProposalSet) -> None:
        active = self.active_ref()
        if value.catalog != active:
            raise ContractError("Le ProposalSet référence un catalogue périmé.")
        for proposal in value.proposals:
            self.validate_preset(proposal)
        if value.decision_report.get("musical_plan"):
            from .rtx.musical import validate_musical_set
            from .intent import ToneIntent
            capabilities = self.capabilities()
            from .knowledge import KNOWLEDGE_VERSION
            if value.decision_report.get("knowledge_version") != KNOWLEDGE_VERSION:
                raise ContractError("Version des connaissances musicales incompatible")
            if value.decision_report.get("capability_sha256") != capabilities["capability_sha256"]:
                raise ContractError("Les connaissances/métadonnées ont changé pendant le travail.")
            try:
                intent = ToneIntent.model_validate(value.decision_report["tone_intent"])
                validate_musical_set(value, capabilities, intent, value.decision_report.get("prompt", ""))
                from .knowledge import adapt_parameters
                profile = None
                profile_id = value.decision_report.get("profile_id")
                if profile_id:
                    with self.database.connect() as c:
                        row = c.execute("SELECT p.*,c.nam_input_calibration_dbu FROM guitar_profiles p LEFT JOIN guitar_calibrations c USING(profile_id) WHERE p.profile_id=?", (profile_id,)).fetchone()
                    if row is None:
                        raise ValueError("Profil musical inconnu")
                    profile = dict(row)
                plugins = {p["plugin_id"]: p for p in capabilities["plugins"]}
                for spec in value.proposals:
                    for step in spec.chain:
                        expected = adapt_parameters(plugins[step.plugin_id], intent, spec.variant, profile)
                        if step.parameters != expected:
                            raise ValueError("Paramètres différents des adaptateurs déterministes du Pi")
            except (ValueError, KeyError, TypeError) as exc:
                raise ContractError(str(exc)) from exc

    def validate_preset(self, spec: PresetSpec) -> None:
        active = self.active_ref()
        if spec.catalog != active:
            raise ContractError("Le PresetSpec référence un catalogue périmé.")
        if len(spec.chain) > self.max_chain_length:
            raise ContractError("La chaîne dépasse la politique CPU du Pi.")
        with self.database.connect() as connection:
            for step in spec.chain:
                plugin = connection.execute(
                    "SELECT * FROM catalog_plugins WHERE revision=? AND plugin_id=?",
                    (active.revision, step.plugin_id),
                ).fetchone()
                if plugin is None:
                    raise ContractError(f"Plugin inconnu : {step.plugin_id}")
                if plugin["uri"] in RENDER_ONLY_URIS:
                    raise ContractError(f"Plugin réservé au banc de rendu : {plugin['name']}")
                descriptor = _descriptor(plugin)
                controls = {
                    port["symbol"]: port
                    for port in descriptor["ports"]
                    if port["kind"] == "control" and port["direction"] == "input"
                }
                for symbol, parameter_value in step.parameters.items():
                    port = controls.get(symbol)
                    if port is None:
                        raise ContractError(f"Paramètre inconnu : {plugin['name']}/{symbol}")
                    self._validate_parameter(plugin["name"], port, parameter_value)
                allowed_roles = PLUGIN_RESOURCE_ROLES.get(plugin["uri"], set())
                supplied_roles = {resource.role for resource in step.resources}
                missing_roles = allowed_roles - supplied_roles
                if missing_roles:
                    raise ContractError(
                        f"Ressource obligatoire absente pour {plugin['name']} : {', '.join(sorted(missing_roles))}"
                    )
                for resource in step.resources:
                    if resource.role not in allowed_roles:
                        raise ContractError(f"{plugin['name']} n'accepte pas {resource.role}.")
                    asset = connection.execute(
                        "SELECT * FROM catalog_assets WHERE revision=? AND asset_id=?",
                        (active.revision, resource.asset_id),
                    ).fetchone()
                    if asset is None:
                        raise ContractError(f"Ressource inconnue : {resource.asset_id}")
                    if asset["kind"] != RESOURCE_KIND_BY_ROLE[resource.role]:
                        raise ContractError(f"Type de ressource incompatible : {resource.asset_id}")

    @staticmethod
    def _validate_parameter(plugin_name: str, port: dict[str, Any], value: ParameterValue) -> None:
        datatype = port["datatype"]
        label = f"{plugin_name}/{port['symbol']}"
        if datatype == "boolean":
            if type(value) is not bool:
                raise ContractError(f"{label} doit être booléen.")
            numeric: int | float = int(value)
        elif datatype in {"integer", "enumeration"}:
            if type(value) is not int:
                raise ContractError(f"{label} doit être entier.")
            numeric = value
            if datatype == "enumeration" and port["scale_points"]:
                allowed = {point["value"] for point in port["scale_points"]}
                if value not in allowed:
                    raise ContractError(f"Énumération refusée : {label}={value}")
        else:
            if type(value) not in {int, float}:
                raise ContractError(f"{label} doit être numérique.")
            numeric = value
        minimum, maximum, default = port["minimum"], port["maximum"], port["default"]
        if minimum is not None and maximum is not None:
            if not (minimum <= numeric <= maximum or numeric == default):
                raise ContractError(f"Valeur hors plage : {label}={numeric}")

    def asset_row(self, revision: int, asset_id: str):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_assets WHERE revision=? AND asset_id=?",
                (revision, asset_id),
            ).fetchone()
            if row is None:
                raise CatalogError(f"Ressource absente : {asset_id}")
            return row

    def plugin_row(self, revision: int, plugin_id: str):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM catalog_plugins WHERE revision=? AND plugin_id=?",
                (revision, plugin_id),
            ).fetchone()
            if row is None:
                raise CatalogError(f"Plugin absent : {plugin_id}")
            return row
