from fastapi import APIRouter, Depends, Request

from ..models import Vessel
from ..services.inspection_store import InspectionStore
from ..services.location_service import list_vessels
from ..auth.deps import require_user
from .deps import get_cache, get_store

# SV-02: every endpoint here needs a signed-in account (viewer, reviewer or administrator).
router = APIRouter(prefix="/api", tags=["vessels"], dependencies=[Depends(require_user)])


@router.get("/vessels", response_model=list[Vessel])
def vessels(request: Request, store: InspectionStore = Depends(get_store)) -> list[Vessel]:
    # Cached per data set: an Excel import or location decision replaces the store, which invalidates it.
    return get_cache(request).get_or_compute(("vessels",), lambda: list_vessels(store), owner=store)
