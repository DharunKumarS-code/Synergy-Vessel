import datetime as dt
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..models import DatesResult, ImagesResult, InspectionRecord, Location, Page, SearchItem, SearchResult
from ..services.inspection_store import InspectionStore, normalise
from ..services.location_service import (
    dates_for,
    filter_records,
    list_locations,
    location_exists,
    matched_fields,
)
from ..auth.deps import require_user
from .deps import check_vessel, get_cache, get_store

# SV-02: every endpoint here needs a signed-in account (viewer, reviewer or administrator).
router = APIRouter(prefix="/api", tags=["inspections"], dependencies=[Depends(require_user)])

LocationQ = Annotated[str, Query(min_length=1, max_length=200, description="Official Location (case-insensitive)")]
VesselQ = Annotated[Optional[str], Query(max_length=100, description="vessel_id from /api/vessels")]
AiQ = Annotated[bool, Query(description="Also match AI Suggested Location (supplementary)")]


def _require_location(store: InspectionStore, location: str, include_ai: bool) -> None:
    if not location.strip():
        raise HTTPException(status_code=422, detail="location must not be blank")
    if not location_exists(store, location, include_ai):
        raise HTTPException(status_code=404, detail=f"location '{location}' not found")


@router.get("/locations", response_model=list[Location])
def locations(request: Request, vessel: VesselQ = None, include_placeholder: bool = True,
              store: InspectionStore = Depends(get_store)) -> list[Location]:
    check_vessel(store, vessel)
    # Cached per data set (see app/infra/cache.py); a reload of the data replaces the store and the entry.
    return get_cache(request).get_or_compute(("locations", vessel, include_placeholder),
                                             lambda: list_locations(store, vessel, include_placeholder), owner=store)


@router.get("/inspections", response_model=Page)
def inspections(
    vessel: VesselQ = None,
    week: Annotated[Optional[str], Query(max_length=50)] = None,
    date: Optional[dt.date] = None,
    location: Annotated[Optional[str], Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    store: InspectionStore = Depends(get_store),
) -> Page:
    check_vessel(store, vessel)
    recs = filter_records(store.records, vessel_id=vessel, week=week, date=date, location=location)
    items = recs[offset: offset + limit]
    return Page(total=len(recs), count=len(items), offset=offset, limit=limit, items=items)


@router.get("/inspections/search", response_model=SearchResult)
def search(
    location: LocationQ,
    vessel: VesselQ = None,
    match: Literal["exact", "contains"] = "exact",
    include_ai_location: AiQ = False,
    store: InspectionStore = Depends(get_store),
) -> SearchResult:
    check_vessel(store, vessel)
    if not location.strip():
        raise HTTPException(status_code=422, detail="location must not be blank")
    placeholder = store.settings.unlabelled_placeholder
    recs = filter_records(store.records, vessel_id=vessel, location=location, match=match,
                          include_ai_location=include_ai_location, placeholder=placeholder)
    want, ph = normalise(location), normalise(placeholder)
    items = [
        SearchItem(**r.model_dump(), matched_on=matched_fields(
            r, want, match=match, include_ai_location=include_ai_location, placeholder=ph))
        for r in recs
    ]
    return SearchResult(location=location, match=match, include_ai_location=include_ai_location,
                        count=len(items), items=items)


@router.get("/inspections/dates", response_model=DatesResult)
def dates(location: LocationQ, vessel: VesselQ = None, include_ai_location: AiQ = False,
          store: InspectionStore = Depends(get_store)) -> DatesResult:
    check_vessel(store, vessel)
    _require_location(store, location, include_ai_location)
    recs = filter_records(store.records, vessel_id=vessel, location=location, include_ai_location=include_ai_location)
    entries, undated = dates_for(recs)
    return DatesResult(location=location, dates=entries, undated_record_count=undated)


@router.get("/inspections/images", response_model=ImagesResult)
def images(location: LocationQ, date: dt.date, vessel: VesselQ = None, include_ai_location: AiQ = False,
           store: InspectionStore = Depends(get_store)) -> ImagesResult:
    check_vessel(store, vessel)
    _require_location(store, location, include_ai_location)
    recs = filter_records(store.records, vessel_id=vessel, location=location, date=date,
                          include_ai_location=include_ai_location)
    return ImagesResult(location=location, date=date, count=len(recs), items=recs)


@router.get("/inspections/{record_id}", response_model=InspectionRecord)
def inspection(record_id: str, store: InspectionStore = Depends(get_store)) -> InspectionRecord:
    rec = store.by_id.get(record_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown record '{record_id}'")
    return rec
