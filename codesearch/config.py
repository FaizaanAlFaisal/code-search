from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    qdrant_url: str
    ollama_url: str
    embed_model: str
    embed_model_ctx: int
    num_gpu: int
    embed_timeout: int
    embed_keep_alive: str
    max_file_bytes: int
    respect_gitignore: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.getenv("CODE_SEARCH_DB_PATH", "storage/code-search/code-search.db")),
            qdrant_url=os.getenv("CODE_SEARCH_QDRANT_URL", os.getenv("QDRANT_URL", "http://127.0.0.1:6333")).rstrip("/"),
            ollama_url=os.getenv("CODE_SEARCH_OLLAMA_URL", os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/"),
            embed_model=os.getenv("CODE_SEARCH_EMBED_MODEL", "qwen3-embedding:4b"),
            embed_model_ctx=_int_env("CODE_SEARCH_EMBED_MODEL_CTX", 8192),
            # 99 = pin all layers to gpu (auto-offload spills ~1gb at 8k); -1 auto, 0 cpu
            num_gpu=_int_env("CODE_SEARCH_NUM_GPU", 99),
            embed_timeout=_int_env("CODE_SEARCH_EMBED_TIMEOUT", 120),
            embed_keep_alive=os.getenv("CODE_SEARCH_EMBED_KEEP_ALIVE", "24h"),
            max_file_bytes=_int_env("CODE_SEARCH_MAX_FILE_BYTES", 524288),
            respect_gitignore=os.getenv("CODE_SEARCH_RESPECT_GITIGNORE", "true").lower() in {"1", "true", "yes"},
        )


settings = Settings.from_env()
