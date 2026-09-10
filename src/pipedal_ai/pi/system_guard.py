from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..errors import ContractError


class SystemGuard:
    def __init__(self, root: Path, max_load_per_cpu: float = 0.90, min_free_mb: int = 256):
        self.root = root
        self.max_load_per_cpu = max_load_per_cpu
        self.min_free_mb = min_free_mb

    def snapshot(self) -> dict[str, float | int | str]:
        cpus = os.cpu_count() or 1
        load = os.getloadavg()[0]
        usage = shutil.disk_usage(self.root if self.root.exists() else self.root.parent)
        temperature = None
        try:
            temperature = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000
        except (OSError, ValueError):
            pass
        return {
            "load_1m": load,
            "cpu_count": cpus,
            "load_per_cpu": load / cpus,
            "free_mb": usage.free // (1024 * 1024),
            "temperature_c": temperature if temperature is not None else "unavailable",
        }

    def admit_background_job(self) -> None:
        state = self.snapshot()
        if float(state["load_per_cpu"]) > self.max_load_per_cpu:
            raise ContractError("Charge système trop élevée : le travail est refusé pour protéger l'audio live.")
        if int(state["free_mb"]) < self.min_free_mb:
            raise ContractError("Espace disque insuffisant pour compiler le preset.")
