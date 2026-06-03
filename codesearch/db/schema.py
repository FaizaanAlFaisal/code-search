from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  root_path TEXT NOT NULL UNIQUE,
  display_name TEXT,
  vcs_type TEXT,
  vcs_head TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  language TEXT,
  parser_name TEXT,
  parser_available INTEGER NOT NULL DEFAULT 0,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  parse_error_count INTEGER NOT NULL DEFAULT 0,
  skipped_reason TEXT,
  indexed_at TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0,
  UNIQUE(repo_id, path)
);

CREATE TABLE IF NOT EXISTS indexing_rules (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  path_kind TEXT NOT NULL CHECK(path_kind IN ('file','dir')),
  action TEXT NOT NULL CHECK(action IN ('include','exclude')),
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(repo_id, path, path_kind, action)
);

CREATE TABLE IF NOT EXISTS path_audit (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('included','excluded','skipped')),
  reason TEXT NOT NULL,
  rule_id INTEGER REFERENCES indexing_rules(id) ON DELETE SET NULL,
  size_bytes INTEGER,
  detected_kind TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(repo_id, path)
);
"""
