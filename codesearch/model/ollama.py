from __future__ import annotations

from typing import Any

import requests

from codesearch.config import settings


def _gpu_options(base: dict[str, Any]) -> dict[str, Any]:
    """Add num_gpu unless it is -1 (let Ollama auto-decide)."""
    if settings.num_gpu >= 0:
        return {**base, "num_gpu": settings.num_gpu}
    return base


def embed_batch(texts: list[str], model: str | None = None) -> tuple[list[list[float]] | None, str | None]:
    # Ollama /api/embed accepts an array `input` and returns one embedding per item.
    # Returns (vectors, None) on success or (None, error) on a request-level failure.
    if not texts:
        return [], None
    options = _gpu_options({"num_ctx": settings.embed_model_ctx})
    if settings.embed_num_batch > 0:
        options["num_batch"] = settings.embed_num_batch
    payload = {
        "model": model or settings.embed_model,
        "input": texts,
        "options": options,
        "keep_alive": settings.embed_keep_alive,
    }
    try:
        response = requests.post(f"{settings.ollama_url}/api/embed", json=payload, timeout=settings.embed_timeout)
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings")
        if embeddings is None:
            single = data.get("embedding")
            embeddings = [single] if single is not None else []
        return embeddings, None
    except Exception as exc:
        return None, str(exc)


def embed(text: str, model: str | None = None) -> tuple[list[float] | None, str | None]:
    vectors, error = embed_batch([text], model)
    if error:
        return None, error
    return (vectors[0] if vectors else None), None


def health() -> bool:
    try:
        response = requests.get(f"{settings.ollama_url}/api/tags", timeout=3)
        return response.ok
    except Exception:
        return False
