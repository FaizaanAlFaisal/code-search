# code-search

A local, low-token code search CLI that combines structural and semantic
search. It indexes a repository with tree-sitter, keeps structure in SQLite
(plus FTS5) and meaning in a local vector store, and answers a query by
returning the source of the most relevant symbols, usually in a single call.

Everything runs on your machine: SQLite for structure, Qdrant for vectors,
Ollama for embeddings. No data leaves the host.

---

## How it works

```
            ┌─────────────┐   tree-sitter    ┌──────────────────────┐
 your repo ─┤  discovery  ├─ extract ───────▶│ SQLite: files,       │
            │  + classify │  symbols/calls   │ symbols, calls, FTS5  │
            └─────────────┘                  └──────────┬───────────┘
                                                        │ embed (Ollama)
                                                        ▼
                                              ┌──────────────────────┐
   query ──▶ FTS5 (BM25) ─┐   Reciprocal     │ Qdrant: symbol/file   │
             vector search ┴── Rank Fusion ──▶│ vectors per repo      │
                                              └──────────────────────┘
```

- Structural index: tree-sitter pulls symbols, call sites, literals, and module
  structure into SQLite. FTS5 (porter unicode61) handles lexical search, and
  identifiers are split into subwords at index time (camelCase, snake_case,
  kebab, SCREAMING), so a fragment like "login" still matches loginAdmin.
- Semantic index: each symbol, file, and module is embedded with Ollama and
  stored in Qdrant.
- Hybrid retrieval: lexical and vector candidates are fused with reciprocal rank
  fusion. Test symbols are demoted and matches are reranked on evidence.
- Optional enrichment: an opt-in summary-model pass (`--enrich`) adds purpose,
  alias, and query hints. It is off by default. The default path is
  deterministic plus embeddings only.

---

## Requirements

- Python 3.10 or newer
- Ollama with the embed model pulled (and the summary model if you use `--enrich`)
- Qdrant (the bundled `docker-compose.yml` brings it up)
- Python packages: `requests`, `tree-sitter`, `tree-sitter-language-pack`,
  `python-dotenv` (see `requirements.txt`)

---

## Quick start

```bash
# 1. backing services (qdrant + ollama)
docker compose up -d
docker compose exec ollama ollama pull qwen3-embedding:4b   # embeddings
docker compose exec ollama ollama pull qwen3.5:4b           # only for --enrich

# 2. python environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. config
cp .env.example .env        # adjust models or urls if needed

# 4. index a repo
./scripts/code-search-admin register /path/to/your/repo myrepo
./scripts/code-search-admin index myrepo

# 5. search
./scripts/code-search explore myrepo "where are http requests retried"
```

The wrapper scripts are relocatable. They find the repo root from their own
location, prefer `./.venv/bin/python`, and load `.env`. Set
`CODE_SEARCH_PYTHON=/path/to/python` to force an interpreter.

---

## Commands

### Retrieval (`code-search`)

| Command | What it does |
|---------|--------------|
| `explore <repo> "<question>"` | Default command. Returns the source of the most relevant symbols grouped by file, plus their callers and callees. One call usually answers the question. |
| `open <repo> <symbol\|r_NN\|path>` | Full body of one symbol with its callers and callees. Use it when a block is truncated. |
| `find <repo> "<token>" [--kind symbol\|call\|literal\|path]` | Every exact definition and call site of a name, literal, or path. |
| `<repo> "<query>"` | Ranked locations only, no source. |
| `refresh <repo>` | Reindex changed files (sha256 and mtime), then re-embed them. |

Add `--json` to any command for structured output.

### Admin (`code-search-admin`)

| Command | What it does |
|---------|--------------|
| `register <path> <slug>` | Map a repo path to a stable slug. |
| `index <slug> [--no-model] [--enrich]` | Index, then run embeddings (and enrichment with `--enrich`). `--no-model` does structure only. |
| `reindex <slug>` | Re-evaluate all paths. |
| `reindex-file <slug> <path>` | Re-evaluate one path. |
| `include-path` / `exclude-path` | Manual include and exclude rules that override the auto-classifier and `.gitignore`. |
| `model-queue` | Pending, running, and done counts per job type, with a remaining and percent summary. |
| `run-queued-jobs [--enrich]` | Run queued model work without reindexing. |
| `status` | DB path, qdrant and ollama health, and the configured models. |
| `list-repos`, `inspect-file`, `inspect-symbol`, `inspect-paths` | Introspection helpers. |

---

## Staleness and refresh

A result is marked `(stale)` when the file changed on disk after it was indexed.
Refreshing is optional. If a stale hit matters for the task, run
`code-search refresh <repo>` and try again. Otherwise keep going and refresh
later. `explore` withholds the body of a stale symbol, since its line numbers no
longer line up, and shows the signature instead.

`refresh` is incremental. It reports something like `reindexed 2 changed of 89
files, 87 unchanged (skipped)`, so you can confirm it is not reprocessing
everything.

---

## Progress and interrupt safety

Long model batches print progress to stderr (`embedding: 1200/24310 (5%)`) and
commit every `CODE_SEARCH_COMMIT_EVERY` jobs (default 50). You can watch a big
index from another shell:

```bash
./scripts/code-search-admin model-queue --json
```

Interrupting a long run keeps the embeddings that already finished. A re-run
picks up the remaining pending jobs.

---

## Configuration

All settings are environment variables with sensible defaults. See
[.env.example](.env.example) for the annotated list: db path, service urls,
models, gpu offload, timeouts, keep-alive, batch size, file-size cap, gitignore
behavior, and progress and commit cadence.
