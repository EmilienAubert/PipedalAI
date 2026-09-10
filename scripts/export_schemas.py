#!/usr/bin/env python3
import json
from pathlib import Path

from pipedal_ai.models import PresetSpec, ProposalSet, RTXProposalRequest, TextPresetJobRequest

TARGETS = {
    "preset-spec-v1.schema.json": PresetSpec,
    "proposal-set-v1.schema.json": ProposalSet,
    "rtx-request-v1.schema.json": RTXProposalRequest,
    "text-job-request-v1.schema.json": TextPresetJobRequest,
}
root = Path(__file__).resolve().parent.parent / "schemas" / "api-v1"
root.mkdir(parents=True, exist_ok=True)
for name, model in TARGETS.items():
    (root / name).write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
