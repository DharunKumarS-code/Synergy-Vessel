"""SV-03: review status transitions and the self-approval rule are enforced by the server.

All tests use temporary analysis / auth folders and the mock AI provider; dashboard/data is never touched.
"""

from __future__ import annotations

import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.ai.providers import MockConditionProvider
from app.ai.records import ReviewStatus
from app.ai.review_policy import MIN_CORRECTION_REASON, ReviewNotAllowed, check_review
from app.config import Settings
from app.main import create_app
from conftest import TEST_PASSWORD

P, A, R, N = "PENDING_REVIEW", "APPROVED", "REJECTED", "NEEDS_REASSESSMENT"
REASON = "Checked again on board; corrected."


# =========================================================================== fixtures / helpers
@pytest.fixture
def c(data_root, tmp_path):
    s = Settings(data_root=data_root, analysis_dir=tmp_path / "analyses", import_dir=tmp_path / "imports",
                 auth_dir=tmp_path / "auth")
    with TestClient(create_app(s, condition_provider=MockConditionProvider("stable"))) as client:
        yield client


def user(c, name: str, role: str = "reviewer") -> dict:
    """Headers for a fresh signed-in account (identity only ever comes from this token)."""
    c.app.state.auth.users.create(name, TEST_PASSWORD, role, f"{role.title()} {name}")
    r = c.post("/api/auth/login", json={"username": name, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def analyse(c, who: dict) -> dict:
    from test_ai_analysis import rid

    body = {"earlier_record_id": rid(c, "W30-0001"), "later_record_id": rid(c, "W34-0001"), "acknowledge_warnings": True}
    r = c.post("/api/ai-analysis/compare", json=body, headers=who)
    assert r.status_code == 200, r.text
    return r.json()["analysis"]


def review(c, aid: str, who: dict, status: str, note: str | None = None):
    return c.post(f"/api/ai-analysis/{aid}/review", json={"status": status, "note": note}, headers=who)


def stored_bytes(c, aid: str) -> bytes:
    return (c.app.state.analysis.store.directory / f"{aid}.json").read_bytes()


def audit(c) -> list[dict]:
    return [json.loads(line) for line in c.app.state.auth.audit.path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def team(c):
    """runner = the reviewer who ran the analysis; other = a second reviewer; admin = an administrator."""
    t = {"runner": user(c, "runner"), "other": user(c, "other"), "admin": user(c, "boss", "admin")}
    t["aid"] = analyse(c, t["runner"])["analysis_id"]
    return t


def decided(c, team, status: str) -> str:
    """The team's analysis, decided once by the second reviewer."""
    if status != P:
        assert review(c, team["aid"], team["other"], status).status_code == 200
    return team["aid"]


# =========================================================================== the policy itself (pure function)
EXPECTED = {  # (current, requested) -> outcome for a reviewer who did NOT run the analysis
    (P, A): "ok", (P, R): "ok", (P, N): "ok", (P, P): "invalid_review_transition",
    (A, R): "review_already_decided", (A, N): "review_already_decided", (A, A): "review_unchanged",
    (R, A): "review_already_decided", (R, N): "review_already_decided", (R, R): "review_unchanged",
    (N, A): "review_already_decided", (N, R): "review_already_decided", (N, N): "review_unchanged",
    (A, P): "invalid_review_transition", (R, P): "invalid_review_transition", (N, P): "invalid_review_transition",
}


def _outcome(current, requested, **kw):
    try:
        check_review(ReviewStatus(current), ReviewStatus(requested), **kw)
        return "ok"
    except ReviewNotAllowed as exc:
        return exc.code


def test_policy_table_for_reviewers_covers_every_combination():
    assert set(EXPECTED) == set(itertools.product((P, A, R, N), repeat=2))
    for (cur, req), want in EXPECTED.items():
        assert _outcome(cur, req, actor_id="u2", actor_is_admin=False, requested_by_id="u1", note=None) == want, (cur, req)


def test_policy_table_for_administrators():
    for cur, req in itertools.product((P, A, R, N), repeat=2):
        got = _outcome(cur, req, actor_id="u2", actor_is_admin=True, requested_by_id="u1", note=REASON)
        if req == P:
            assert got == "invalid_review_transition"            # never back to Awaiting review, not even an admin
        elif req == cur:
            assert got == "review_unchanged"
        else:
            assert got == "ok"                                    # first decision, or a correction with a reason
    assert _outcome(A, R, actor_id="u2", actor_is_admin=True, requested_by_id=None, note=None) == "correction_reason_required"
    short = "x" * (MIN_CORRECTION_REASON - 1)
    assert _outcome(A, R, actor_id="u2", actor_is_admin=True, requested_by_id=None, note=f"  {short}  ") == "correction_reason_required"


def test_policy_self_approval():
    kw = dict(actor_id="u1", requested_by_id="u1", note=None)
    assert _outcome(P, A, actor_is_admin=False, **kw) == "self_approval_not_allowed"
    assert _outcome(P, R, actor_is_admin=False, **kw) == "ok"     # rejecting / reassessing your own work is fine
    assert _outcome(P, N, actor_is_admin=False, **kw) == "ok"
    d = check_review(ReviewStatus(P), ReviewStatus(A), actor_is_admin=True, **kw)
    assert d.self_review is True and d.correction is False        # administrators may, flagged
    # unknown runner (records saved before SV-03): no self-approval check is possible
    assert _outcome(P, A, actor_id="u1", actor_is_admin=False, requested_by_id=None, note=None) == "ok"


# =========================================================================== valid transitions through the API
@pytest.mark.parametrize("decision", [A, R, N])
def test_pending_can_be_decided_by_a_reviewer(c, team, decision):
    r = review(c, team["aid"], team["other"], decision, "looked at both photos")
    assert r.status_code == 200
    rec = r.json()
    assert rec["review"]["status"] == decision
    ev = rec["review"]["history"][-1]
    assert (ev["status"], ev["previous_status"], ev["correction"], ev["self_review"]) == (decision, P, False, False)
    last = audit(c)[-1]
    assert last["event"] == "analysis_reviewed" and last["target"] == team["aid"]
    assert last["details"] == {"decision": decision, "previous_status": P, "note_provided": True}


# =========================================================================== refused transitions change nothing
REFUSED = [  # (current, requested, reviewer, expected HTTP status, code)
    (A, R, "other", 409, "review_already_decided"),          # 4. Approved -> Rejected
    (A, P, "other", 409, "invalid_review_transition"),       # 5. Approved -> Awaiting review
    (R, A, "other", 409, "review_already_decided"),          # 6. Rejected -> Approved
    (R, P, "other", 409, "invalid_review_transition"),       # 7. Rejected -> Awaiting review
    (R, N, "other", 409, "review_already_decided"),          # 8. Rejected -> Needs reassessment
    (N, A, "other", 409, "review_already_decided"),          # 9. Needs reassessment -> Approved
    (N, P, "other", 409, "invalid_review_transition"),       # 10. Needs reassessment -> Awaiting review
    (A, N, "other", 409, "review_already_decided"),
    (N, R, "other", 409, "review_already_decided"),
    (A, A, "other", 409, "review_unchanged"),
    (P, P, "other", 409, "invalid_review_transition"),
    (A, P, "admin", 409, "invalid_review_transition"),       # not even an administrator
    (R, A, "admin", 422, "correction_reason_required"),      # admin correction needs a reason
    (P, A, "runner", 403, "self_approval_not_allowed"),      # reviewer approving their own analysis
]


@pytest.mark.parametrize("current,requested,who,status,code", REFUSED)
def test_refused_transitions_change_nothing(c, team, current, requested, who, status, code):
    aid = decided(c, team, current)
    before, events_before = stored_bytes(c, aid), len(audit(c))
    r = review(c, aid, team[who], requested)
    assert (r.status_code, r.json()["code"]) == (status, code)
    assert r.json()["detail"] and "Traceback" not in r.text
    assert stored_bytes(c, aid) == before                          # record (status + history) untouched on disk
    assert c.app.state.analysis.store.get(aid).review.status.value == current   # and in memory
    assert len(audit(c)) == events_before                          # no review event recorded
    assert c.get(f"/api/ai-analysis/{aid}", headers=team["admin"]).json()["review"]["status"] == current


def test_unknown_analysis_and_unauthenticated_requests_are_still_refused(c, team):
    assert review(c, "CA-20260101-0000000000", team["other"], A).status_code == 404
    assert c.post(f"/api/ai-analysis/{team['aid']}/review", json={"status": A}).status_code == 401


# =========================================================================== administrator corrections
def test_admin_correction_is_appended_and_audited(c, team):
    aid = decided(c, team, R)
    r = review(c, aid, team["admin"], A, REASON)
    assert r.status_code == 200
    hist = r.json()["review"]["history"]
    assert [h["status"] for h in hist] == [R, A]                   # earlier decision kept, never overwritten
    assert hist[0]["reviewer_username"] == "other" and hist[0]["correction"] is False
    assert (hist[1]["previous_status"], hist[1]["correction"], hist[1]["reviewer_role"], hist[1]["note"]) == (R, True, "admin", REASON)
    ev = audit(c)[-1]
    assert ev["event"] == "analysis_review_corrected" and ev["actor"]["username"] == "boss"
    assert ev["details"] == {"decision": A, "previous_status": R, "note_provided": True, "reason": REASON}
    assert c.get("/api/admin/audit/verify", headers=team["admin"]).json()["valid"] is True
    # a second correction is possible (with a reason), but a reviewer still cannot change it
    assert review(c, aid, team["other"], N).json()["code"] == "review_already_decided"
    assert review(c, aid, team["admin"], N, "Photos unclear after all, re-analyse.").status_code == 200


def test_admin_first_decision_needs_no_reason(c, team):
    assert review(c, team["aid"], team["admin"], R).status_code == 200


# =========================================================================== self-approval
def test_runner_is_recorded_from_the_session(c, team):
    rec = c.get(f"/api/ai-analysis/{team['aid']}", headers=team["admin"]).json()   # SV-02: admins see the runner
    assert rec["requested_by"] == {"user_id": c.app.state.auth.users.by_username("runner").user_id,
                                   "username": "runner", "role": "reviewer"}


def test_runner_cannot_be_supplied_in_the_request(c, team):
    from test_ai_analysis import rid

    body = {"earlier_record_id": rid(c, "W30-0003"), "later_record_id": rid(c, "W34-0001"), "acknowledge_warnings": True,
            "requested_by": {"user_id": "someone-else", "username": "x", "role": "admin"}}
    assert c.post("/api/ai-analysis/compare", json=body, headers=team["runner"]).status_code == 422


def test_reviewer_cannot_approve_own_analysis_even_by_naming_someone_else(c, team):
    r = c.post(f"/api/ai-analysis/{team['aid']}/review", json={"status": A, "reviewer": "Other Reviewer"},
               headers=team["runner"])
    assert r.status_code == 403 and r.json()["code"] == "self_approval_not_allowed"
    assert review(c, team["aid"], team["other"], A).status_code == 200      # another reviewer can


@pytest.mark.parametrize("decision", [R, N])
def test_reviewer_can_reject_or_reassess_own_analysis(c, team, decision):
    assert review(c, team["aid"], team["runner"], decision).status_code == 200


def test_admin_may_approve_own_analysis_and_it_is_flagged(c, team):
    from test_ai_analysis import rid

    r = c.post("/api/ai-analysis/compare", headers=team["admin"], json={
        "earlier_record_id": rid(c, "W30-0003"), "later_record_id": rid(c, "W34-0001"), "acknowledge_warnings": True})
    assert r.status_code == 200, r.text
    aid = r.json()["analysis"]["analysis_id"]
    r = review(c, aid, team["admin"], A)
    assert r.status_code == 200 and r.json()["review"]["history"][-1]["self_review"] is True
    ev = audit(c)[-1]
    assert ev["event"] == "analysis_reviewed" and ev["details"]["self_review"] is True


def test_records_saved_before_sv03_have_no_runner_and_stay_approvable(c, team):
    store = c.app.state.analysis.store
    legacy = store.get(team["aid"]).model_copy(deep=True)
    legacy.requested_by = None                     # temporary test store only: mimics a pre-SV-03 record
    store.save(legacy)
    raw = json.loads(stored_bytes(c, team["aid"]))
    assert raw.get("requested_by") is None
    assert review(c, team["aid"], team["runner"], A).status_code == 200   # runner unknown: no self-check possible


# =========================================================================== concurrency
def test_a_pending_assessment_cannot_be_decided_twice(c, team):
    """Every decision is checked against the SAVED status (inside the service lock), so a second, late decision
    on the same assessment is refused instead of silently replacing the first."""
    first = review(c, team["aid"], team["other"], A)
    second = review(c, team["aid"], team["admin"], R)                       # admin, but no correction reason
    assert first.status_code == 200 and second.status_code == 422
    assert c.get(f"/api/ai-analysis/{team['aid']}", headers=team["other"]).json()["review"]["history"][-1]["status"] == A
