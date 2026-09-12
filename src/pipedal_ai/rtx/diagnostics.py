from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path


logger = logging.getLogger(__name__)
MAX_TEXT_CHARS = 1_000_000
MAX_RECORDS = 32


def save_exchange(directory: Path | None, stage: str, attempt: int, request: dict,
                  response: str | None, status: int | None, error: str | None) -> str | None:
    """Opt-in local traces, never authentication headers or environment secrets.

    Prompts/catalog metadata can be private. Keep at most 32 bounded records;
    disabling tracing never changes whether a model answer is accepted.
    """
    if directory is None:
        return None
    try:
        if directory.is_symlink():
            raise OSError("Diagnostic directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        trace_id = uuid.uuid4().hex
        path = directory / f"ollama-{trace_id}.json"
        request_text = json.dumps(request, ensure_ascii=False, allow_nan=False)
        response_text = response or ""
        record = {
            "trace_id": trace_id, "stage": stage, "attempt": attempt,
            "http_status": status, "error": error,
            "request_json": request_text[:MAX_TEXT_CHARS],
            "request_truncated": len(request_text) > MAX_TEXT_CHARS,
            "response_raw": response_text[:MAX_TEXT_CHARS],
            "response_truncated": len(response_text) > MAX_TEXT_CHARS,
        }
        with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(record, stream, ensure_ascii=False, allow_nan=False)
        records = sorted(
            (p for p in directory.glob("ollama-*.json") if p.is_file() and not p.is_symlink()),
            key=lambda p: p.stat().st_mtime_ns,
        )
        for old in records[:-MAX_RECORDS]:
            # Only UUID-named files created by this trace writer are eligible.
            if len(old.stem) == 39 and all(c in "0123456789abcdef" for c in old.stem[7:]):
                old.unlink()
        logger.info("Ollama diagnostic stage=%s attempt=%s trace=%s", stage, attempt, trace_id)
        return trace_id
    except (OSError, ValueError) as exc:
        logger.warning("Unable to save Ollama diagnostic: %s", exc)
        return None
