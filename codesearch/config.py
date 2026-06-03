from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    qdrant_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            qdrant_url=os.getenv("CODE_SEARCH_QDRANT_URL", os.getenv("QDRANT_URL", "http://127.0.0.1:6333")).rstrip("/"),
        )


settings = Settings.from_env()
