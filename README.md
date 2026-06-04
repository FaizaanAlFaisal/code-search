# code-search

A fast, low-token, **semantic + structural** code search CLI built for AI agents (and humans). It indexes a repository with tree-sitter, stores structure in SQLite (+ FTS5) and meaning in a local vector store, and answers questions by returning the *actual source of the most relevant symbols* — usually in a single call — instead of making an agent grep-and-read its way around.

It runs fully locally: SQLite for structure, **Qdrant** for vectors, **Ollama** for embeddings. No data leaves your machine.

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

- **Structural index** — tree-sitter extracts symbols, call sites, literals, and module structure into SQLite. FTS5 (`porter unicode61`) gives BM25 lexical search, with identifier subword tokenization (camelCase / snake_case / kebab / SCREAMING all split at index time).
- **Semantic index** — each symbol/file/module is embedded (Ollama) and stored in Qdrant.
- **Hybrid retrieval** — lexical and vector candidates are fused with Reciprocal Rank Fusion, with test symbols demoted and evidence-based reranking.
- **Optional enrichment** — an opt-in summary-model pass (`--enrich`) adds purpose/alias/query hints. Off by default; the default path is deterministic + embeddings only.

---

## Requirements

- Python **3.10+**
- [Ollama](https://ollama.com/) with the embed model pulled
- [Qdrant](https://qdrant.tech/) (via the bundled `docker-compose.yml`)
- Python deps: `requests` (see `requirements.txt`)

---

## Quick start

```bash
# 1. Backing services (Qdrant + Ollama)
docker compose up -d
docker compose exec ollama ollama pull qwen3-embedding:4b   # embeddings

# 2. Python environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Config
cp .env.example .env        # tweak models / URLs if needed
```
