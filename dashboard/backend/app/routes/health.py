from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends

from ..services.inspection_store import InspectionStore
from ..auth.deps import CurrentUser, RequireAdmin
from .deps import get_store

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(user: CurrentUser, store: InspectionStore = Depends(get_store)) -> dict[str, Any]:
    """Public (monitoring, the "API connected" indicator before sign-in). SV-02: anonymous callers get the
    status only; record counts and load time are for signed-in users."""
    if user is None:
        return {"status": "ok"}
    t = store.report.totals
    return {
        "status": "ok",
        "weeks_loaded": t.get("weeks_readable", 0),
        "records": t.get("total_appearances", 0),
        "resolved_images": t.get("resolved_images", 0),
        "loaded_at": store.report.loaded_at,
    }


@router.get("/validation")
def validation(_: RequireAdmin, store: InspectionStore = Depends(get_store)) -> dict[str, Any]:
    """Integration report: per-week counts, missing/invalid records, issues. SV-02: administrators only, and
    never the absolute data folder path."""
    report = asdict(store.report)
    report.pop("data_root", None)
    return report
