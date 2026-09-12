"""Explicit feedback, scoped to a guitar profile and content-hashed assets."""
from __future__ import annotations
import json
from typing import Literal
from pydantic import Field
from .models import StrictModel, ProposalSet
from .pi.jobs import now


class Feedback(StrictModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    variant: Literal["conservative", "balanced", "bold"]
    label: Literal["preferred", "too_saturated", "too_bright", "too_dark", "noisy"]


def record_feedback(database, catalog, value):
    with database.connect() as c:
        job = c.execute("SELECT * FROM jobs WHERE job_id=?", (value.job_id,)).fetchone()
    if not job or job["status"] != "completed":
        raise ValueError("Travail terminé requis")
    proposal = ProposalSet.model_validate_json(job["proposal_json"])
    spec = next(s for s in proposal.proposals if s.variant == value.variant)
    assets = []
    for step in spec.chain:
        for resource in step.resources:
            row = catalog.asset_row(proposal.catalog.revision, resource.asset_id)
            assets.append({"asset_id": resource.asset_id, "asset_sha256": row["sha256"]})
    identifier = database.new_id("pref")
    with database.transaction() as c:
        c.execute("INSERT INTO preferences VALUES(?,?,?,?,?,?,?)", (identifier, value.job_id, value.variant,
                  value.label, job["profile_id"], database.json({"assets": assets}), now()))
    return {"preference_id": identifier}


def ranking_preferences(database, profile_id):
    # No named guitar profile means no inferred universal preference.
    if not profile_id:
        return []
    with database.connect() as c:
        rows = list(c.execute("SELECT label,features_json FROM preferences WHERE profile_id=? ORDER BY created_at DESC LIMIT 64", (profile_id,)))
    scores = {}
    for row in rows:
        for asset in json.loads(row["features_json"])["assets"]:
            sha = asset["asset_sha256"]
            scores[sha] = scores.get(sha, 0) + (2 if row["label"] == "preferred" else -1)
    return [{"asset_sha256": sha, "score": max(-8, min(8, score))} for sha, score in list(scores.items())[:64]]
