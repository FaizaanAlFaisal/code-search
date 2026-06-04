from __future__ import annotations

import json
import time
from typing import Any

import requests

from codesearch.config import settings


def _gpu_options(base: dict[str, Any]) -> dict[str, Any]:
    """Add num_gpu unless it is -1 (let Ollama auto-decide)."""
    if settings.num_gpu >= 0:
        return {**base, "num_gpu": settings.num_gpu}
    return base


def generate_json(prompt: str, model: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    payload = {
        "model": model or settings.summary_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": _gpu_options({"num_ctx": settings.summary_model_ctx}),
        "think": settings.summary_model_think,
        "keep_alive": settings.summary_keep_alive,
    }
    try:
        response = requests.post(f"{settings.ollama_url}/api/generate", json=payload, timeout=settings.summary_timeout)
        response.raise_for_status()
        raw = response.json().get("response", "{}")
        return json.loads(raw), None
    except Exception as exc:
        return None, str(exc)


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


def unload_model(model: str) -> str | None:
    payload = {"model": model, "prompt": "", "stream": False, "keep_alive": 0}
    try:
        response = requests.post(f"{settings.ollama_url}/api/generate", json=payload, timeout=30)
        response.raise_for_status()
        return None
    except Exception as exc:
        return str(exc)


def loaded_models() -> tuple[set[str], str | None]:
    try:
        response = requests.get(f"{settings.ollama_url}/api/ps", timeout=5)
        response.raise_for_status()
        return {row.get("model") or row.get("name") for row in response.json().get("models", [])}, None
    except Exception as exc:
        return set(), str(exc)


def wait_until_unloaded(model: str, timeout_seconds: float = 60.0, poll_seconds: float = 1.0) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        models, error = loaded_models()
        if error:
            last_error = error
        elif model not in models:
            return None
        time.sleep(poll_seconds)
    return last_error or f"{model} still loaded after {timeout_seconds:.0f}s"


def health() -> bool:
    try:
        response = requests.get(f"{settings.ollama_url}/api/tags", timeout=3)
        return response.ok
    except Exception:
        return False
