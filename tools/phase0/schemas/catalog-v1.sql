PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS catalog_revisions (
    revision                 INTEGER PRIMARY KEY,
    catalog_sha256           TEXT NOT NULL UNIQUE CHECK(length(catalog_sha256) = 64),
    catalog_schema_version   TEXT NOT NULL,
    inventory_schema_version TEXT NOT NULL,
    source_inventory_sha256  TEXT NOT NULL CHECK(length(source_inventory_sha256) = 64),
    collector_version        TEXT NOT NULL,
    generated_at             TEXT NOT NULL,
    imported_at              TEXT NOT NULL,
    plugin_count             INTEGER NOT NULL CHECK(plugin_count >= 0),
    asset_count              INTEGER NOT NULL CHECK(asset_count >= 0),
    warning_count            INTEGER NOT NULL CHECK(warning_count >= 0)
);

CREATE TABLE IF NOT EXISTS catalog_plugins (
    revision          INTEGER NOT NULL,
    plugin_id         TEXT NOT NULL,
    uri               TEXT NOT NULL,
    name              TEXT,
    class             TEXT,
    author            TEXT,
    has_latency       INTEGER,
    descriptor_sha256 TEXT NOT NULL CHECK(length(descriptor_sha256) = 64),
    descriptor_json   TEXT NOT NULL,
    PRIMARY KEY (revision, plugin_id),
    UNIQUE (revision, uri),
    FOREIGN KEY (revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS plugin_ports (
    revision              INTEGER NOT NULL,
    plugin_id             TEXT NOT NULL,
    port_index            INTEGER NOT NULL CHECK(port_index >= 0),
    symbol                TEXT NOT NULL,
    name                  TEXT,
    direction             TEXT,
    kind                  TEXT NOT NULL,
    datatype              TEXT NOT NULL,
    minimum               REAL,
    maximum               REAL,
    default_value         REAL,
    properties_json       TEXT NOT NULL,
    scale_points_json     TEXT NOT NULL,
    supported_events_json TEXT NOT NULL,
    PRIMARY KEY (revision, plugin_id, port_index),
    UNIQUE (revision, plugin_id, symbol),
    FOREIGN KEY (revision, plugin_id)
        REFERENCES catalog_plugins(revision, plugin_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS catalog_assets (
    revision      INTEGER NOT NULL,
    asset_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    extension     TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL CHECK(size_bytes >= 0),
    sha256        TEXT NOT NULL CHECK(length(sha256) = 64),
    PRIMARY KEY (revision, asset_id),
    UNIQUE (revision, relative_path),
    FOREIGN KEY (revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS catalog_warnings (
    revision     INTEGER NOT NULL,
    warning_index INTEGER NOT NULL CHECK(warning_index >= 0),
    severity     TEXT NOT NULL,
    code         TEXT NOT NULL,
    warning_json TEXT NOT NULL,
    PRIMARY KEY (revision, warning_index),
    FOREIGN KEY (revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS catalog_state (
    singleton       INTEGER PRIMARY KEY CHECK(singleton = 1),
    active_revision INTEGER NOT NULL,
    FOREIGN KEY (active_revision) REFERENCES catalog_revisions(revision) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_catalog_plugins_name
    ON catalog_plugins(revision, name);
CREATE INDEX IF NOT EXISTS idx_catalog_assets_kind
    ON catalog_assets(revision, kind);
