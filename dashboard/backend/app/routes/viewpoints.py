"""SV-08 viewpoint mapping endpoints.

Read-only (any signed-in account; no file-system paths are ever returned):
GET  /api/inspections/viewpoints?location=...[&vessel=...]   viewpoints of one Official Location, week by week
GET  /api/inspections/viewpoints/groups                        display-only location groups ("Main Deck Areas")
GET  /api/inspections/viewpoints/pair?earlier=..&later=..      how two records relate through the mapping
GET  /api/inspections/viewpoints/record/{record_id}            the viewpoint a record belongs to

Administration (administrators only; every decision is audited and kept in the mapping history):
GET  /api/admin/viewpoints/review?location=..&week=..&status=..
GET  /api/admin/viewpoints/history[?viewpoint_id=..]
POST /api/admin/viewpoints/decisions        approve | approve_all_proposed | reject | reinstate | move | split | join | rename
"""

from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from ..auth.deps import RequireAdmin, get_auth, require_user
from ..services.viewpoints import STATUSES, ViewpointError, ViewpointService
from .deps import check_vessel, get_store

router = APIRouter(prefix="/api/inspections/viewpoints", tags=["viewpoints"], dependencies=[Depends(require_user)])
admin_router = APIRouter(prefix="/api/admin/viewpoints", tags=["viewpoints (admin)"])

RecordQ = Annotated[str, Query(min_length=1, max_length=96)]


def get_viewpoints(request: Request) -> ViewpointService:
    svc = getattr(request.app.state, "viewpoints", None)
    if svc is None:
        raise ViewpointError(503, "not_ready", "The viewpoint service is not ready.")
    return svc


Svc = Annotated[ViewpointService, Depends(get_viewpoints)]


@router.get("")
def viewpoints_for_location(svc: Svc, request: Request,
                            location: Annotated[str, Query(min_length=1, max_length=200)],
                            vessel: Annotated[Optional[str], Query(max_length=100)] = None) -> dict:
    if not location.strip():
        raise ViewpointError(422, "invalid_location", "location must not be blank")
    check_vessel(get_store(request), vessel)
    return svc.for_location(location, vessel)


@router.get("/groups")
def viewpoint_groups(svc: Svc, vessel: Annotated[Optional[str], Query(max_length=100)] = None) -> dict:
    return svc.groups(vessel)


@router.get("/pair")
def viewpoint_pair(svc: Svc, earlier: RecordQ, later: RecordQ) -> dict:
    return svc.pair_status(earlier, later)


@router.get("/record/{record_id}")
def viewpoint_of_record(svc: Svc, record_id: str) -> dict:
    return svc.lookup(record_id[:96])


# --------------------------------------------------------------------------- administration
@admin_router.get("/review")
def review(svc: Svc, _: RequireAdmin,
           location: Annotated[Optional[str], Query(max_length=200)] = None,
           week: Annotated[Optional[int], Query(ge=1, le=60)] = None,
           status: Annotated[Optional[Literal[STATUSES]], Query()] = None) -> dict:  # type: ignore[valid-type]
    return svc.review(location or None, week, status)


@admin_router.get("/history")
def history(svc: Svc, _: RequireAdmin, viewpoint_id: Annotated[Optional[str], Query(max_length=80)] = None,
            limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict:
    return svc.history(viewpoint_id, limit)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "approve_all_proposed", "reject", "reinstate", "move", "split", "join", "rename"]
    revision: int = Field(ge=1, description="The mapping revision the decision was made on (optimistic concurrency)")
    viewpoint_id: str = Field(min_length=4, max_length=80)
    record_ids: list[str] = Field(default_factory=list, max_length=200)
    target_viewpoint_id: Optional[str] = Field(default=None, max_length=80)
    at_week: Optional[int] = Field(default=None, ge=1, le=60)
    display_name: Optional[str] = Field(default=None, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=500)


@admin_router.post("/decisions")
async def decide(body: Decision, svc: Svc, actor: RequireAdmin, request: Request) -> dict:
    audit = get_auth(request).audit
    try:
        result = await run_in_threadpool(svc.decide, body.action, body.model_dump(), actor.actor())
    except ViewpointError as exc:
        audit.record("viewpoint_decision_failed", actor=actor.actor(), target=body.viewpoint_id,
                     details={"action": body.action, "code": exc.code, "record_ids": body.record_ids[:20]})
        raise
    entry = result["history_entry"]
    audit.record("viewpoint_decided", actor=actor.actor(), target=body.viewpoint_id,
                 details={"action": body.action, "revision": result["revision"], "history_seq": entry["seq"],
                          "viewpoint_ids": entry["viewpoint_ids"], "record_ids": body.record_ids[:50],
                          "changes": entry["changes"][:50], "notes": entry["notes"]})
    return result
