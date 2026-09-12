"""Local, explicitly enabled PiPedal maintenance renderer. No RTX write access."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
import json
import math
import sys
from time import monotonic
import uuid
import zipfile
from pathlib import Path

from websockets.asyncio.client import connect

from ..audio_io import atomic_json, read_audio, safe_path, write_pcm
from ..compiler import ATOM_PATH
from ..errors import ContractError, RemoteServiceError
from ..knowledge import TOOB


RENDERER_VERSION = "pipedal-ai.pipedal-bench/1.0.0"


@contextmanager
def process_lock(root):
    """A Pi-local lock shared by CLI and web service processes."""
    if not sys.platform.startswith("linux"):
        raise ContractError("Le banc de rendu PiPedal nécessite Linux sur le Raspberry Pi")
    import fcntl
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = safe_path(root, ".render.lock", exists=False)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError("Un autre banc PiPedal est déjà actif") from exc
        yield
    finally:
        os.close(descriptor)


def board_signature(board):
    """Ignore host counters, retain routing, controls, state and master levels."""
    keys = ("name", "items", "input_volume_db", "output_volume_db")
    return json.dumps({k: board.get(k) for k in keys}, sort_keys=True)


def instance_ids(board):
    result = []
    def walk(items):
        for item in items:
            result.append(int(item.get("instanceId", 0)))
            walk(item.get("topChain", [])); walk(item.get("bottomChain", []))
    walk(board.get("items", []))
    return result


def verify_bench(expected, actual):
    if any(actual.get(k) != expected.get(k) for k in ("name", "input_volume_db", "output_volume_db")):
        raise ContractError("Le pedalboard ou les niveaux du banc ont changé")
    nodes = {n["instanceId"]: n for n in actual.get("items", [])}
    if set(nodes) != {n["instanceId"] for n in expected["items"]}:
        raise ContractError("Routage du banc différent du plan")
    for item in expected["items"]:
        node = nodes[item["instanceId"]]
        if node.get("uri") != item["uri"] or node.get("isEnabled") != item["isEnabled"]:
            raise ContractError("Plugin du banc différent du plan")
        controls = {c["key"]: c["value"] for c in node.get("controlValues", [])}
        for control in item["controlValues"]:
            if item["uri"] in (TOOB+"player", TOOB+"record-mono", TOOB+"record-stereo") and control["key"] in ("play", "stop", "pause", "record"):
                continue
            received, expected_value = controls.get(control["key"]), control["value"]
            equal = (math.isclose(float(received), float(expected_value), rel_tol=1e-5, abs_tol=5e-5)
                     if isinstance(received, (int, float)) and isinstance(expected_value, (int, float))
                     else received == expected_value)
            if not equal:
                raise ContractError("Paramètre du banc modifié : " + control["key"])


def file_property(item, uri, path):
    value = str(path)
    item["lv2State"] = [True, {uri: {"flags": 3, "atomType": ATOM_PATH, "value": value}}]
    item["pathProperties"] = {uri: json.dumps({"otype_": "Path", "value": value})}
    item["stateUpdateCount"] = 1


def utility_item(plugin, identifier, parameters, property_uri, path):
    controls = {p["symbol"]: p["default"] for p in json.loads(plugin["descriptor_json"])["ports"]
                if p["kind"] == "control" and p["direction"] == "input" and p["default"] is not None}
    if set(parameters) - set(controls):
        raise ContractError("Version du plugin de banc incompatible")
    controls.update(parameters)
    item = {"instanceId": identifier, "uri": plugin["uri"], "pluginName": plugin["name"],
            "isEnabled": True, "controlValues": [{"key": k, "value": v} for k, v in controls.items()],
            "midiBindings": [], "midiChannelBinding": None, "stateUpdateCount": 0,
            "lv2State": [False, {}], "lilvPresetUri": "", "pathProperties": {},
            "title": "", "useModUi": False, "iconColor": "", "sideChainInputId": -1}
    file_property(item, property_uri, path)
    return item


class PiPedalRenderer:
    def __init__(self, catalog, compiler, client, config, guard):
        self.catalog, self.compiler, self.client, self.config, self.guard = catalog, compiler, client, config, guard
        self.lock = asyncio.Lock()

    async def render(self, spec, di_path: Path, target: Path, *, maintenance_confirmed: bool):
        if not self.config.enabled or not maintenance_confirmed:
            raise ContractError("Banc désactivé ou séance hors live non confirmée")
        with process_lock(self.config.output_root):
            return await self._render(spec, di_path, target, maintenance_confirmed=maintenance_confirmed)

    async def _render(self, spec, di_path: Path, target: Path, *, maintenance_confirmed: bool):
        if not self.config.enabled or not maintenance_confirmed:
            raise ContractError("Banc désactivé ou séance hors live non confirmée")
        self.guard.admit_background_job()
        self.catalog.validate_preset(spec)
        data, rate = read_audio(di_path, require_di=True)
        if len(data) / rate + self.config.tail_seconds > 180:
            raise ContractError("DI trop longue pour le banc")
        # The archive compiler verifies every model/IR against its content hash.
        token = uuid.uuid4().hex
        work = self.config.output_root / ("render-" + token)
        work.mkdir(parents=True, mode=0o700)
        artifact = self.compiler.compile(spec, work)
        with zipfile.ZipFile(artifact["path"]) as archive:
            board = json.loads(archive.read("bankFile.json"))["presets"][0]["preset"]
        utilities = {}
        with self.catalog.database.connect() as c:
            for uri in (TOOB + "player", TOOB + "record-mono", TOOB + "record-stereo"):
                row = c.execute("SELECT * FROM catalog_plugins WHERE revision=? AND uri=?",
                                (spec.catalog.revision, uri)).fetchone()
                if row:
                    utilities[uri] = row
        stereo = any(p["audio_outputs"] > 1 for p in self.catalog.capabilities()["plugins"]
                     if p["plugin_id"] in {s.plugin_id for s in spec.chain})
        record_uri = TOOB + ("record-stereo" if stereo else "record-mono")
        if TOOB + "player" not in utilities or record_uri not in utilities:
            raise ContractError("TooB File Player / Record Input absent du catalogue")
        upload_root = self.compiler.upload_root
        track_relative = f"{self.config.track_directory}/PiPedalAI/{token}.wav"
        record_relative = f"{self.config.record_directory}/PiPedalAI/{token}.wav"
        track = safe_path(upload_root, track_relative, exists=False)
        recorded = safe_path(upload_root, record_relative, exists=False)
        write_pcm(track, data, rate)
        recorded.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        resources = {self.catalog.asset_row(spec.catalog.revision, r.asset_id)["relative_path"]:
                     self.catalog.asset_row(spec.catalog.revision, r.asset_id) for s in spec.chain for r in s.resources}
        for item in board["items"]:
            item["instanceId"] += 1
            if item["lv2State"][0]:
                for uri, state in item["lv2State"][1].items():
                    asset = resources.get(state["value"])
                    if asset is None:
                        raise ContractError("État fichier absent des ressources validées")
                    # Reuse the compiler's explicit trust policy for factory IR
                    # symlinks, instead of allowing arbitrary external links.
                    self.compiler._verified_asset(asset["relative_path"], asset["size_bytes"], asset["sha256"])
                    path = (upload_root / asset["relative_path"]).absolute()
                    state["value"] = str(path)
                    item["pathProperties"][uri] = json.dumps({"otype_": "Path", "value": str(path)})
        player = utility_item(utilities[TOOB + "player"], 1,
                              {"volIn": -40, "volFile": 0, "play": 0},
                              TOOB + "player#audioFile", track)
        recorder_id = len(board["items"]) + 2
        recorder = utility_item(utilities[record_uri], recorder_id,
                                {"record": 0, "fformat": 0, "level": 0},
                                TOOB + "record#audioFile", recorded)
        board["items"] = [player, *board["items"], recorder]
        board["nextInstanceId"] = recorder_id + 1
        board["name"] = "PiPedal AI bench " + token
        # Recorder is before master output attenuation. Physical monitoring is muted.
        board["input_volume_db"] = 0
        board["output_volume_db"] = -96
        board["snapshots"] = []
        board["selectedSnapshot"] = -1
        journal = work / "restore.json"
        async with self.lock:
            async with connect(self.client.config.websocket_url, open_timeout=5, close_timeout=2) as ws:
                sequence = 1
                async def request(message, body=None):
                    nonlocal sequence
                    sequence += 1
                    return await self.client._request(ws, message, body, sequence)
                async def send(message, body):
                    await ws.send(json.dumps([{"message": message}, body]))
                async def control(instance, symbol, value):
                    await send("setControl", {"clientId": -1, "instanceId": instance, "symbol": symbol, "value": value})
                previous = await request("currentPedalboard")
                # PiPedal's structure update reuses effects by instance ID alone.
                # Fresh IDs are essential: otherwise the previous live effects
                # could be borrowed instead of instantiating the requested plugins.
                first = max([int(previous.get("nextInstanceId", 1)), *instance_ids(previous)]) + 1
                for index, item in enumerate(board["items"]):
                    item["instanceId"] = first + index
                player_id, recorder_id = first, first + len(board["items"]) - 1
                board["nextInstanceId"] = recorder_id + 1
                atomic_json(journal, {"previous": previous, "bench_name": board["name"], "status": "armed"})
                telemetry = []
                try:
                    await send("updateCurrentPedalboard", {"clientId": -1, "pedalboard": board})
                    current = await request("currentPedalboard")
                    if current.get("name") != board["name"]:
                        raise RemoteServiceError("PiPedal n'a pas chargé le banc")
                    verify_bench(board, current)
                    await asyncio.sleep(0.5)
                    before = await request("getJackStatus")
                    if not before.get("active", False):
                        raise RemoteServiceError("Moteur audio PiPedal inactif")
                    if before.get("sampleRate", 48000) != 48000:
                        raise ContractError("Banc prévu uniquement à 48 kHz")
                    if before.get("cpuUsage", 0) > self.config.max_audio_cpu_percent:
                        raise ContractError("Charge du moteur trop élevée avant lecture")
                    await control(recorder_id, "record", 1)
                    await asyncio.sleep(0.1)
                    await control(player_id, "play", 1)
                    deadline = monotonic() + len(data) / rate + self.config.tail_seconds
                    while monotonic() < deadline:
                        await asyncio.sleep(min(0.5, max(0.01, deadline - monotonic())))
                        status = await request("getJackStatus")
                        telemetry.append(status)
                        if status.get("underruns", 0) > before.get("underruns", 0):
                            raise ContractError("Décrochage audio pendant le rendu")
                        if status.get("cpuUsage", 0) > self.config.max_audio_cpu_percent:
                            raise ContractError("Charge du moteur audio trop élevée")
                        current = await request("currentPedalboard")
                        if current.get("name") != board["name"]:
                            raise ContractError("Banc interrompu par un changement utilisateur")
                        verify_bench(board, current)
                        if self.catalog.active_ref() != spec.catalog:
                            raise ContractError("Catalogue modifié pendant le rendu")
                    await control(player_id, "stop", 1)
                    await control(recorder_id, "stop", 1)
                    await request("currentPedalboard")
                    await asyncio.sleep(0.3)
                finally:
                    # Also restore if the original websocket fails, using a new local
                    # connection. Never replace a different user-selected board.
                    try:
                        async with connect(self.client.config.websocket_url, open_timeout=5) as restore:
                            current = await self.client._request(restore, "currentPedalboard", sequence=1)
                            if current.get("name") == board["name"]:
                                for instance, symbol in ((player_id, "stop"), (recorder_id, "stop")):
                                    await restore.send(json.dumps([{"message": "setControl"},
                                        {"clientId": -1, "instanceId": instance, "symbol": symbol, "value": 1}]))
                                await restore.send(json.dumps([{"message": "updateCurrentPedalboard"},
                                    {"clientId": -1, "pedalboard": previous}]))
                                verified = await self.client._request(restore, "currentPedalboard", sequence=2)
                                if board_signature(verified) != board_signature(previous):
                                    raise RemoteServiceError("Restauration du pedalboard non confirmée")
                            atomic_json(journal, {"status": "restored-or-user-changed"}, overwrite=True)
                    except Exception as exc:
                        raise RemoteServiceError(f"Restauration du banc échouée; journal={journal}: {exc}") from exc
                output, output_rate = read_audio(recorded)
                if len(output) / output_rate < len(data) / rate * 0.9:
                    raise ContractError("Enregistrement trop court")
                write_pcm(target, output, output_rate)
                return {"renderer_version": RENDERER_VERSION, "physical_output_db": -96,
                        "telemetry": telemetry, "restoration": "verified", "journal": str(journal)}

    async def recover(self, journal_path):
        """Explicit local recovery after a crash; never overwrite a user's new board."""
        root = self.config.output_root.resolve(strict=True)
        path = safe_path(root, Path(journal_path).absolute().relative_to(root).as_posix())
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("status") != "armed" or not value.get("previous"):
            raise ContractError("Journal non armé")
        with process_lock(root):
            async with connect(self.client.config.websocket_url, open_timeout=5) as ws:
                current = await self.client._request(ws, "currentPedalboard", sequence=1)
                if current.get("name") != value["bench_name"]:
                    raise ContractError("Le pedalboard courant a changé : restauration automatique refusée")
                await ws.send(json.dumps([{"message": "updateCurrentPedalboard"},
                                         {"clientId": -1, "pedalboard": value["previous"]}]))
                verified = await self.client._request(ws, "currentPedalboard", sequence=2)
                if board_signature(verified) != board_signature(value["previous"]):
                    raise RemoteServiceError("Restauration non confirmée")
                atomic_json(path, {"status": "recovered"}, overwrite=True)
                return {"restored": True}
