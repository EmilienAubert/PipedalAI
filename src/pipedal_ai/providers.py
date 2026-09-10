from __future__ import annotations

from typing import Protocol


class AssetProvider(Protocol):
    """Contract for future Tone3000 or local asset providers."""

    async def refresh(self) -> int: ...


class DisabledTone3000Provider:
    async def refresh(self) -> int:
        return 0
