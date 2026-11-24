"""Phase 7 Excel import endpoints. The workbook is sent as the raw request body (no multipart dependency).

POST /api/imports/preview?filename=...                 validate + classify; writes nothing
POST /api/imports?filename=...&fingerprint=<sha256>    commit exactly the previewed file (atomic)
GET  /api/imports                                      import history (+ failed attempts)
GET  /api/imports/{import_id}                          one import with per-row results
GET  /api/locations/review                             Phase 10.1: pending location reviews (read-only, signed in)
GET|POST /api/locations/synonyms, PUT|DELETE .../{id}   Phase 10.3: managed location synonyms (admin only)
POST /api/locations/review/group-decision              Phase 10.5: whole-group decision (admin; dry_run previews)
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from ..auth.deps import RequireAdmin, get_auth
from ..imports.parser import MAX_UPLOAD_BYTES
from ..imports.service import ImportFailure, ImportService

router = APIRouter(prefix="/api/imports", tags=["imports"])
review_router = APIRouter(prefix="/api/locations", tags=["location review"])


def get_imports(request: Request) -> ImportService:
    svc = getattr(request.app.state, "imports", None)
    if svc is None:
        raise ImportFailure(503, "not_ready", "The import service is not ready.")
    return svc


Svc = Annotated[ImportService, Depends(get_imports)]


async def _body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise ImportFailure(413, "file_too_large", "The workbook is too large to import.",
                            "Split the register into smaller workbooks and import them one at a time.")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ImportFailure(413, "file_too_large", "The workbook is too large to import.",
                                "Split the register into smaller workbooks and import them one at a time.")
    return bytes(data)


@router.post("/preview")
async def preview(request: Request, svc: Svc, _: RequireAdmin, filename: str = Query(..., min_length=1, max_length=255)) -> dict:
    return await run_in_threadpool(svc.preview, await _body(request), filename)


@router.post("")
async def commit(request: Request, svc: Svc, actor: RequireAdmin, filename: str = Query(..., min_length=1, max_length=255),
                 fingerprint: str = Query(..., pattern=r"^[0-9a-fA-F]{64}$")) -> dict:
    audit = get_auth(request).audit
    try:
        result = await run_in_threadpool(svc.commit, await _body(request), filename, fingerprint, actor.actor())
    except ImportFailure as exc:
        # reload_failed means the import WAS saved (it appears after a restart): record it as committed.
        audit.record("import_committed" if exc.code == "reload_failed" else "import_rejected", actor=actor.actor(),
                     target=filename[:200], details={"code": exc.code, "status": exc.status})
        raise
    audit.record("import_committed", actor=actor.actor(), target=result["import_id"],
                 details={"filename": result["filename"], "source_sha256": result["source_sha256"],
                          "imported": (result.get("summary") or {}).get("imported")})
    return result


@router.get("")
def history(svc: Svc, _: RequireAdmin) -> dict:  # SV-02: import management -> admin
    return svc.history()


@router.get("/location-ai")
def location_ai_status(svc: Svc, _: RequireAdmin) -> dict:
    """Phase 9: whether location AI is available (provider, model, simulated). Never includes the API key."""
    return svc.location_intel.status()


@router.get("/{import_id}")
def get_import(import_id: str, svc: Svc, _: RequireAdmin) -> dict:
    return svc.get(import_id)


# ---------------------------------------------------------------------- Phase 8.4: integrity + reversible rollback
class StateChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    confirm_import_id: str = Field(min_length=1, max_length=40)


@router.get("/{import_id}/verify")
def verify_import(import_id: str, svc: Svc, request: Request, actor: RequireAdmin) -> dict:
    result = svc.verify(import_id)
    get_auth(request).audit.record("import_verified", actor=actor.actor(), target=import_id,
                                   details={"valid": result["valid"], "problems": len(result["problems"])})
    return result


def _affected_analyses(request: Request, import_id: str) -> list[str]:
    prefix = f"{import_id}-R"
    return sorted(a.analysis_id for a in request.app.state.analysis.store.all()
                  if a.earlier.record_id.startswith(prefix) or a.later.record_id.startswith(prefix))


@router.post("/{import_id}/withdraw")
async def withdraw_import(import_id: str, body: StateChange, svc: Svc, request: Request, actor: RequireAdmin) -> dict:
    audit = get_auth(request).audit
    affected = _affected_analyses(request, import_id)
    try:
        result = await run_in_threadpool(lambda: svc.withdraw(import_id, reason=body.reason, confirm=body.confirm_import_id,
                                                              actor=actor.actor(), affected_analyses=affected))
    except ImportFailure as exc:
        audit.record("import_withdraw_failed", actor=actor.actor(), target=import_id, details={"code": exc.code})
        raise
    audit.record("import_withdrawn", actor=actor.actor(), target=import_id,
                 details={"reason": body.reason[:200], "affected_analyses": affected})
    return result


@router.post("/{import_id}/reinstate")
async def reinstate_import(import_id: str, body: StateChange, svc: Svc, request: Request, actor: RequireAdmin) -> dict:
    audit = get_auth(request).audit
    try:
        result = await run_in_threadpool(lambda: svc.reinstate(import_id, reason=body.reason, confirm=body.confirm_import_id,
                                                               actor=actor.actor()))
    except ImportFailure as exc:
        audit.record("import_reinstate_failed", actor=actor.actor(), target=import_id, details={"code": exc.code})
        raise
    audit.record("import_reinstated", actor=actor.actor(), target=import_id, details={"reason": body.reason[:200]})
    return result


# ---------------------------------------------------------------------- Phase 9: location review (admin)
class LocationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    row_number: int = Field(ge=1, le=100000)
    action: str = Field(pattern="^(accept|reject|keep_original|manual)$")
    location: str | None = Field(default=None, max_length=200)
    # A 'manual' location that is not already trusted is refused unless the administrator explicitly confirms that it
    # is a NEW location (it then becomes trusted). Mirrors the preview/confirm step of group decisions.
    confirm_new_location: bool = False


@router.post("/{import_id}/locations")
async def decide_location(import_id: str, body: LocationDecision, svc: Svc, request: Request, actor: RequireAdmin) -> dict:
    audit = get_auth(request).audit
    try:
        result = await run_in_threadpool(lambda: svc.decide_location(import_id, body.row_number, body.action, body.location,
                                                                     actor=actor.actor(),
                                                                     confirm_new_location=body.confirm_new_location))
    except ImportFailure as exc:
        audit.record("import_location_decision_failed", actor=actor.actor(), target=import_id,
                     details={"row": body.row_number, "action": body.action, "code": exc.code})
        raise
    audit.record("import_location_decided", actor=actor.actor(), target=import_id,
                 details={"row": body.row_number, "action": body.action,
                          "location": (result["effective_location"]["location"] or "")[:120]})
    return result


# ---------------------------------------------------------------------- Phase 10.1: read-only review queue
@review_router.get("/review")
def location_review_queue(svc: Svc, _: RequireAdmin) -> dict:
    """Pending location decisions across active committed imports, grouped by original text. Read-only: it never
    calls AI, takes no lock and writes nothing. SV-02 (F1): administrators only -- the queue is import management
    data (import ids, filenames, source cells, location-AI suggestions); the decisions are admin-only as well."""
    return svc.review_queue()


# ---------------------------------------------------------------------- Phase 10.3: managed synonyms (admin only)
class SynonymBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    synonym: str = Field(max_length=200)
    canonical: str = Field(max_length=200)


def _synonym_audit(request: Request, event: str, actor, target: str, details: dict) -> None:
    get_auth(request).audit.record(event, actor=actor.actor(), target=target,
                                   details={k: (v[:120] if isinstance(v, str) else v) for k, v in details.items()})


@review_router.get("/synonyms")
def list_location_synonyms(svc: Svc, _: RequireAdmin) -> dict:
    return svc.list_synonyms()


@review_router.post("/synonyms", status_code=201)
async def create_location_synonym(body: SynonymBody, svc: Svc, request: Request, actor: RequireAdmin) -> dict:
    try:
        entry = await run_in_threadpool(lambda: svc.create_synonym(body.synonym, body.canonical, actor.actor()))
    except ImportFailure as exc:
        _synonym_audit(request, "location_synonym_create_failed", actor, "synonyms",
                       {"synonym": body.synonym, "canonical": body.canonical, "code": exc.code})
        raise
    _synonym_audit(request, "location_synonym_created", actor, entry["id"],
                   {"synonym": entry["synonym"], "canonical": entry["canonical"]})
    return {"synonym": entry}


@review_router.put("/synonyms/{synonym_id}")
async def update_location_synonym(synonym_id: str, body: SynonymBody, svc: Svc, request: Request,
                                  actor: RequireAdmin) -> dict:
    try:
        result = await run_in_threadpool(lambda: svc.update_synonym(synonym_id, body.synonym, body.canonical, actor.actor()))
    except ImportFailure as exc:
        _synonym_audit(request, "location_synonym_update_failed", actor, synonym_id[:40],
                       {"synonym": body.synonym, "canonical": body.canonical, "code": exc.code})
        raise
    prev, entry = result["previous"], result["synonym"]
    _synonym_audit(request, "location_synonym_updated", actor, synonym_id,
                   {"synonym": entry["synonym"], "canonical": entry["canonical"],
                    "previous_synonym": prev["synonym"], "previous_canonical": prev["canonical"]})
    return {"synonym": entry}


@review_router.delete("/synonyms/{synonym_id}")
async def delete_location_synonym(synonym_id: str, svc: Svc, request: Request, actor: RequireAdmin) -> dict:
    try:
        entry = await run_in_threadpool(lambda: svc.delete_synonym(synonym_id))
    except ImportFailure as exc:
        _synonym_audit(request, "location_synonym_delete_failed", actor, synonym_id[:40], {"code": exc.code})
        raise
    _synonym_audit(request, "location_synonym_deleted", actor, synonym_id,
                   {"synonym": entry["synonym"], "canonical": entry["canonical"]})
    return {"deleted": entry}


# ---------------------------------------------------------------------- Phase 10.5: whole-group decisions (admin only)
class GroupDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_key: str = Field(max_length=200)
    action: str = Field(max_length=20)
    location: str | None = Field(default=None, max_length=200)
    expected_count: int | None = Field(default=None, ge=0, le=100000)
    dry_run: bool = False


@review_router.post("/review/group-decision")
async def group_location_decision(body: GroupDecision, svc: Svc, request: Request, actor: RequireAdmin) -> dict:
    if body.dry_run:                                      # preview for the confirmation step: no lock, no write, no audit
        return await run_in_threadpool(lambda: svc.preview_group_decision(body.group_key, body.action, body.location))
    audit = get_auth(request).audit
    try:
        if body.expected_count is None:
            raise ImportFailure(400, "expected_count_required", "Confirm the number of records to decide.",
                                "Preview the group first.")
        result = await run_in_threadpool(lambda: svc.decide_group(body.group_key, body.action, body.location,
                                                                  body.expected_count, actor=actor.actor()))
    except ImportFailure as exc:
        audit.record("location_group_decision_failed", actor=actor.actor(), target=body.group_key[:120],
                     details={"action": body.action, "location": (body.location or "")[:120], "code": exc.code,
                              "expected_count": body.expected_count})
        raise
    audit.record("location_group_decided", actor=actor.actor(), target=result["group_decision_id"],
                 details={"group_key": result["group_key"][:120], "action": result["action"],
                          "location": (result["location"] or "")[:120], "resolved_via": result["resolved_via"],
                          "new_location": result["new_location"], "count": len(result["decided_rows"]),
                          "records": [{"import_id": r["import_id"], "row": r["row_number"],
                                       "original": (r["original_location"] or "")[:120]} for r in result["decided_rows"][:500]],
                          "excluded": result["excluded_reasons"]})
    return result
