from __future__ import annotations

import requests

from codesearch.config import settings


def qdrant_health() -> bool:
    try:
        return requests.get(f"{settings.qdrant_url}/collections", timeout=3).ok
    except Exception:
        return False
