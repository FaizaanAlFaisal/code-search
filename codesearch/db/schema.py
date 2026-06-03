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

CREATE TABLE IF NOT EXISTS file_facts (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  fact_type TEXT NOT NULL,
  value TEXT NOT NULL,
  line_start INTEGER,
  line_end INTEGER,
  confidence TEXT NOT NULL DEFAULT 'primary'
);

CREATE TABLE IF NOT EXISTS symbols (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  symbol_ref TEXT NOT NULL,
  symbol_type TEXT NOT NULL,
  signature TEXT,
  decorators TEXT,
  docstring TEXT,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end INTEGER NOT NULL,
  parent_symbol_id INTEGER REFERENCES symbols(id),
  exported INTEGER NOT NULL DEFAULT 0,
  primary_text TEXT NOT NULL,
  UNIQUE(file_id, qualified_name, line_start)
);

CREATE TABLE IF NOT EXISTS symbol_calls (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  call_text TEXT NOT NULL,
  receiver TEXT,
  function_name TEXT,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  resolved_symbol_id INTEGER REFERENCES symbols(id),
  resolution_status TEXT NOT NULL DEFAULT 'unresolved'
);

CREATE TABLE IF NOT EXISTS symbol_literals (
  id INTEGER PRIMARY KEY,
  symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
  literal_type TEXT NOT NULL,
  value TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS heuristic_flags (
  id INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  flag TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  evidence_value TEXT NOT NULL,
  line_start INTEGER,
  line_end INTEGER,
  rule_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_jobs (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  job_type TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  input_hash TEXT NOT NULL,
  prompt_tokens_estimate INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS model_outputs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES model_jobs(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  output_type TEXT NOT NULL,
  json_text TEXT NOT NULL,
  accepted INTEGER NOT NULL DEFAULT 0,
  rejected_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vector_records (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  vector_collection TEXT NOT NULL,
  vector_point_id TEXT NOT NULL UNIQUE,
  text_hash TEXT NOT NULL,
  text_kind TEXT NOT NULL,
  embedded_model TEXT NOT NULL,
  embedded_at TEXT NOT NULL
);
"""
