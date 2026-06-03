from __future__ import annotations

from typing import Any

import requests

from codesearch.config import settings


def _gpu_options(base: dict[str, Any]) -> dict[str, Any]:
    """Add num_gpu unless it is -1 (let Ollama auto-decide)."""
    if settings.num_gpu >= 0:
        return {**base, "num_gpu": settings.num_gpu}
    return base


def embed(text: str, model: str | None = None) -> tuple[list[float] | None, str | None]:
    payload = {
        "model": model or settings.embed_model,
        "input": text,
        "options": _gpu_options({"num_ctx": settings.embed_model_ctx}),
        "keep_alive": settings.embed_keep_alive,
    }
    try:
        response = requests.post(f"{settings.ollama_url}/api/embed", json=payload, timeout=settings.embed_timeout)
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings") or []
        if embeddings:
            return embeddings[0], None
        return data.get("embedding"), None
    except Exception as exc:
        return None, str(exc)


def health() -> bool:
    try:
        response = requests.get(f"{settings.ollama_url}/api/tags", timeout=3)
        return response.ok
    except Exception:
        return False
