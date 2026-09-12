from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import CatalogError


CATALOG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_revisions (
 revision INTEGER PRIMARY KEY, catalog_sha256 TEXT NOT NULL UNIQUE,
 catalog_schema_version TEXT NOT NULL, inventory_schema_version TEXT NOT NULL,
 source_inventory_sha256 TEXT NOT NULL, collector_version TEXT NOT NULL,
 generated_at TEXT NOT NULL, imported_at TEXT NOT NULL,
 plugin_count INTEGER NOT NULL, asset_count INTEGER NOT NULL, warning_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_plugins (
 revision INTEGER NOT NULL, plugin_id TEXT NOT NULL, uri TEXT NOT NULL, name TEXT,
 class TEXT, author TEXT, has_latency INTEGER, descriptor_sha256 TEXT NOT NULL,
 descriptor_json TEXT NOT NULL, PRIMARY KEY(revision,plugin_id), UNIQUE(revision,uri),
 FOREIGN KEY(revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS plugin_ports (
 revision INTEGER NOT NULL, plugin_id TEXT NOT NULL, port_index INTEGER NOT NULL,
 symbol TEXT NOT NULL, name TEXT, direction TEXT, kind TEXT NOT NULL, datatype TEXT NOT NULL,
 minimum REAL, maximum REAL, default_value REAL, properties_json TEXT NOT NULL,
 scale_points_json TEXT NOT NULL, supported_events_json TEXT NOT NULL,
 PRIMARY KEY(revision,plugin_id,port_index), UNIQUE(revision,plugin_id,symbol),
 FOREIGN KEY(revision,plugin_id) REFERENCES catalog_plugins(revision,plugin_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS catalog_assets (
 revision INTEGER NOT NULL, asset_id TEXT NOT NULL, kind TEXT NOT NULL,
 relative_path TEXT NOT NULL, extension TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, PRIMARY KEY(revision,asset_id), UNIQUE(revision,relative_path),
 FOREIGN KEY(revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS catalog_warnings (
 revision INTEGER NOT NULL, warning_index INTEGER NOT NULL, severity TEXT NOT NULL,
 code TEXT NOT NULL, warning_json TEXT NOT NULL, PRIMARY KEY(revision,warning_index),
 FOREIGN KEY(revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS catalog_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), active_revision INTEGER NOT NULL,
 FOREIGN KEY(active_revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);
"""

APP_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS guitar_profiles (
 profile_id TEXT PRIMARY KEY, name TEXT NOT NULL, guitar TEXT NOT NULL,
 pickup TEXT NOT NULL, input_trim_db REAL NOT NULL, notes TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL,
 prompt TEXT NOT NULL, profile_id TEXT, catalog_revision INTEGER NOT NULL,
 catalog_sha256 TEXT NOT NULL, request_json TEXT NOT NULL, proposal_json TEXT,
 error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, fallback_reason TEXT,
 FOREIGN KEY(profile_id) REFERENCES guitar_profiles(profile_id),
 FOREIGN KEY(catalog_revision) REFERENCES catalog_revisions(revision)
);
CREATE TABLE IF NOT EXISTS artifacts (
 artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, variant TEXT NOT NULL,
 preset_name TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, imported_instance_id INTEGER,
 activated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(job_id,variant), FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
"""

APP_SCHEMA_V3_SQL = """
CREATE TABLE IF NOT EXISTS asset_metadata (
 revision INTEGER NOT NULL, asset_id TEXT NOT NULL, source TEXT NOT NULL,
 metadata_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(revision,asset_id,source),
 FOREIGN KEY(revision,asset_id) REFERENCES catalog_assets(revision,asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_asset_metadata_revision ON asset_metadata(revision,asset_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path.expanduser().absolute()
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        if self.path.is_symlink():
            raise CatalogError("La base SQLite ne doit pas être un lien symbolique.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = self.path.exists()
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise CatalogError(f"Version SQLite incompatible : {version}")
            if version == 0:
                connection.executescript(CATALOG_SCHEMA_SQL)
            connection.executescript(APP_SCHEMA_SQL)
            connection.executescript(APP_SCHEMA_V3_SQL)
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
                if "fallback_reason" not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN fallback_reason TEXT")
                connection.execute("PRAGMA user_version=4")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if not existed:
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def active_catalog(self, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        query = """SELECT r.* FROM catalog_state s JOIN catalog_revisions r
                   ON r.revision=s.active_revision WHERE s.singleton=1"""
        if connection is not None:
            row = connection.execute(query).fetchone()
        else:
            with self.connect() as owned:
                row = owned.execute(query).fetchone()
        if row is None:
            raise CatalogError("Aucune révision de catalogue active.")
        return row

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
