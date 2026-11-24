from typing import Optional

from fastapi import HTTPException, Request

from ..infra.cache import TTLCache
from ..services.inspection_store import InspectionStore

_NO_CACHE = TTLCache(ttl_seconds=0)


def get_store(request: Request) -> InspectionStore:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="data not loaded")
    return store


def get_cache(request: Request) -> TTLCache:
    """The app's read-only summary cache (a pass-through cache if none is configured)."""
    return getattr(request.app.state, "cache", None) or _NO_CACHE


def check_vessel(store: InspectionStore, vessel: Optional[str]) -> None:
    if vessel is not None and not any(r.vessel_id == vessel for r in store.records):
        raise HTTPException(status_code=404, detail=f"unknown vessel '{vessel}'")
