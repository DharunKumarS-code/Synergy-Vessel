"""Phase 8.4 -- import integrity: cross-process lock, failure injection at every stage, reversible rollback
(withdraw / reinstate), integrity verification, corrupt-manifest duplicates. Temporary folders only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time

import pytest

from app.imports import service as service_mod
from app.imports import store as store_mod
from app.imports.store import ImportLock, LockBusy
from conftest import jpeg_bytes, sign_in
from test_imports import GOOD, commit, make, row, tree, workbook  # noqa: F401  (fixture reuse)


def imports_dir(c):
    return c.app.state.imports.dir


def committed(c):
    return sorted(p.name for p in imports_dir(c).iterdir() if store_mod.IMPORT_ID_RE.match(p.name)) if imports_dir(c).is_dir() else []


def record_ids(c, import_id):
    return sorted(r.record_id for r in c.app.state.store.records if r.import_id == import_id)


def do_commit(c, rows=GOOD, data=None):
    # openpyxl stamps the save time into the file: reuse `data` when the exact same bytes matter
    r = commit(c, data or workbook(rows))
    assert r.status_code == 200, r.text
    return r.json()["import_id"]


def change(c, import_id, action, reason="entered against the wrong vessel", confirm=None):
    return c.post(f"/api/imports/{import_id}/{action}", json={"reason": reason, "confirm_import_id": confirm or import_id})


def audit_events(c):
    return [e["event"] for e in reversed(c.app.state.auth.audit.entries(limit=1000))]


# ===================================================================== failure injection during commit
@pytest.mark.parametrize("stage", ["staging_dir", "source_copy", "image_copy", "manifest", "publish"])
def test_failure_at_any_stage_leaves_no_partial_import(make, data_root, monkeypatch, stage):
    c = make()
    prod_before = tree(data_root)
    records_before = len(c.app.state.store.records)
    real_write, real_rename = service_mod.write_file, os.rename
    calls = {"n": 0}

    def failing_write(path, data):
        name = path.name
        if (stage == "source_copy" and name == "source.xlsx") or (stage == "manifest" and name == "manifest.json") \
                or (stage == "image_copy" and path.parent.name == "images"):
            raise OSError(f"injected failure at {stage}")
        return real_write(path, data)

    if stage == "staging_dir":
        real_mkdir = service_mod.Path.mkdir

        def failing_mkdir(self, *a, **k):
            if any(part.startswith(".staging-") for part in self.parts):
                raise OSError("injected mkdir")
            return real_mkdir(self, *a, **k)

        monkeypatch.setattr(service_mod.Path, "mkdir", failing_mkdir)
    elif stage == "publish":
        monkeypatch.setattr(service_mod.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError("injected rename")))
    else:
        monkeypatch.setattr(service_mod, "write_file", failing_write)
    r = commit(c, workbook(GOOD))
    monkeypatch.undo()
    assert r.status_code == 500 and r.json()["code"] == "import_failed", r.text
    assert committed(c) == []                                         # nothing published
    assert not any(p.name.startswith(".staging-") for p in imports_dir(c).iterdir()) if imports_dir(c).is_dir() else True
    assert not (imports_dir(c) / ".import.lock").exists()            # lock released
    assert len(c.app.state.store.records) == records_before
    assert tree(data_root) == prod_before
    assert audit_events(c)[-1] == "import_rejected"
    # the same workbook can then be imported normally (no duplicate leftovers)
    assert commit(c, workbook(GOOD)).status_code == 200 and len(committed(c)) == 1


def test_reload_failure_after_save_is_reported_and_audited_as_committed(make, monkeypatch):
    c = make()
    svc = c.app.state.imports
    real = svc._reload
    svc._reload = lambda: (_ for _ in ()).throw(RuntimeError("reload broke"))
    r = commit(c, workbook(GOOD))
    svc._reload = real
    assert r.status_code == 500 and r.json()["code"] == "reload_failed"
    assert len(committed(c)) == 1 and audit_events(c)[-1] == "import_committed"
    real()
    assert len(record_ids(c, committed(c)[0])) == 3                  # appears after a reload/restart


# ===================================================================== cross-process lock
def test_lock_excludes_a_second_holder_and_is_released(tmp_path):
    a = ImportLock(tmp_path, timeout=0.2).acquire()
    with pytest.raises(LockBusy):
        ImportLock(tmp_path, timeout=0.2).acquire()
    a.release()
    assert not (tmp_path / ".import.lock").exists()
    with ImportLock(tmp_path, timeout=0.2):
        assert (tmp_path / ".import.lock").exists()
    assert not (tmp_path / ".import.lock").exists()


def test_stale_lock_is_taken_over_but_fresh_lock_is_respected(tmp_path):
    lock = tmp_path / ".import.lock"
    lock.write_text(json.dumps({"token": "crashed-holder"}))
    with pytest.raises(LockBusy):
        ImportLock(tmp_path, timeout=0.2, stale_after=600).acquire()      # fresh -> respected
    old = time.time() - 3600
    os.utime(lock, (old, old))
    got = ImportLock(tmp_path, timeout=0.5, stale_after=600).acquire()    # stale -> taken over
    assert json.loads(lock.read_text())["token"] == got.token
    got.release()


def test_release_never_removes_someone_elses_lock(tmp_path):
    a = ImportLock(tmp_path).acquire()
    (tmp_path / ".import.lock").write_text(json.dumps({"token": "other-process"}))  # e.g. after a stale takeover
    a.release()
    assert (tmp_path / ".import.lock").exists()


def test_commit_refuses_while_another_process_holds_the_lock(make, monkeypatch):
    c = make()
    imports_dir(c).mkdir(parents=True, exist_ok=True)
    other = ImportLock(imports_dir(c)).acquire()                          # simulates a second backend process
    monkeypatch.setattr(service_mod, "ImportLock", lambda d: ImportLock(d, timeout=0.2))
    r = commit(c, workbook(GOOD))
    assert r.status_code == 409 and r.json()["code"] == "import_busy"
    assert committed(c) == []
    other.release()
    assert commit(c, workbook(GOOD)).status_code == 200


def test_another_processes_staging_is_not_deleted_while_it_holds_the_lock(make, monkeypatch):
    """Previously every commit deleted all .staging-* folders, including another process's work in progress."""
    c = make()
    d = imports_dir(c)
    d.mkdir(parents=True, exist_ok=True)
    foreign = d / ".staging-IMP-20261119-aaaaaaaaaa"
    foreign.mkdir()
    holder = ImportLock(d).acquire()
    monkeypatch.setattr(service_mod, "ImportLock", lambda d: ImportLock(d, timeout=0.2))
    assert commit(c, workbook(GOOD)).status_code == 409
    assert foreign.exists()
    holder.release()
    assert commit(c, workbook(GOOD)).status_code == 200
    assert not foreign.exists()                                           # cleaned only once it is safe


def test_concurrent_commits_of_the_same_workbook_create_one_import(make):
    c = make()
    data = workbook(GOOD)
    results = []
    threads = [threading.Thread(target=lambda: results.append(commit(c, data).status_code)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(200) == 1 and len(committed(c)) == 1
    assert len([r for r in c.app.state.store.records if r.source == "excel_import"]) == 3


# ===================================================================== interrupted imports / manifests
def test_interrupted_import_is_ignored_then_cleaned(make):
    c = make()
    d = imports_dir(c)
    d.mkdir(parents=True, exist_ok=True)
    stale = d / ".staging-IMP-20261119-bbbbbbbbbb"
    (stale / "images").mkdir(parents=True)
    (stale / "source.xlsx").write_bytes(workbook(GOOD))                     # files copied, manifest never written
    c.app.state.imports._reload()
    assert not [r for r in c.app.state.store.records if r.source == "excel_import"]
    assert c.get("/api/imports").json()["interrupted_imports"] == 1
    do_commit(c)                                                              # same rows import cleanly, once
    assert not stale.exists() and len([r for r in c.app.state.store.records if r.source == "excel_import"]) == 3


def test_workbook_behind_a_corrupt_manifest_cannot_be_imported_twice(make):
    c = make()
    data = workbook(GOOD)
    iid = do_commit(c, data=data)
    (imports_dir(c) / iid / "manifest.json").write_text("{broken", encoding="utf-8")
    c.app.state.imports._reload()
    r = commit(c, data)
    assert r.status_code == 409 and r.json()["code"] == "duplicate_workbook" and "cannot be read" in r.json()["detail"]
    assert any(iid in p for p in c.get("/api/imports").json()["problems"])


# ===================================================================== integrity verification
def test_verify_detects_missing_modified_and_unexpected_files(make):
    c = make()
    iid = do_commit(c)
    assert c.get(f"/api/imports/{iid}/verify").json() == {"import_id": iid, "valid": True, "state": "active",
                                                          "photographs_checked": 3, "records": 3, "problems": []}
    folder = imports_dir(c) / iid
    imgs = sorted((folder / "images").iterdir())
    imgs[0].unlink()
    imgs[1].write_bytes(imgs[1].read_bytes()[:-10] + b"0" * 10)
    (folder / "images" / "planted.jpg").write_bytes(b"x")
    (folder / "source.xlsx").write_bytes(b"PK changed")
    res = c.get(f"/api/imports/{iid}/verify").json()
    text = "\n".join(res["problems"])
    assert not res["valid"] and "is missing" in text and "does not match its SHA-256" in text
    assert "unexpected file in images/: planted.jpg" in text and "source workbook does not match" in text
    assert "import_verified" in audit_events(c)


def test_verify_and_rollback_require_admin(make):
    c = make()
    iid = do_commit(c)
    sign_in(c, "reviewer")
    for r in (c.get(f"/api/imports/{iid}/verify"), change(c, iid, "withdraw"), change(c, iid, "reinstate")):
        assert r.status_code == 403
    c.headers.pop("Authorization")
    assert change(c, iid, "withdraw").status_code == 401


# ===================================================================== rollback (withdraw / reinstate)
def test_withdraw_restores_the_pre_import_state_and_keeps_files(make, data_root):
    c = make()
    prod_before = tree(data_root)
    base = sorted(r.record_id for r in c.app.state.store.records)
    first = do_commit(c, GOOD)
    second = do_commit(c, [row(9, "W43-0009.jpg", photo=jpeg_bytes(109))])
    files_before = tree(imports_dir(c) / first)
    r = change(c, first, "withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    assert record_ids(c, first) == [] and len(record_ids(c, second)) == 1             # unrelated import untouched
    assert sorted(r.record_id for r in c.app.state.store.records) == sorted(base + record_ids(c, second))
    assert {k: v for k, v in tree(imports_dir(c) / first).items() if k != "status.json"} == files_before
    assert c.get(f"/api/images/{first}-R0002").status_code == 404
    detail = c.get(f"/api/imports/{first}").json()
    assert detail["status"] == "withdrawn" and detail["state_events"][-1]["reason"] == "entered against the wrong vessel"
    assert tree(data_root) == prod_before
    # reinstate brings exactly the same records back
    assert change(c, first, "reinstate", reason="vessel confirmed correct").status_code == 200
    assert len(record_ids(c, first)) == 3
    events = c.get(f"/api/imports/{first}").json()["state_events"]
    assert [e["action"] for e in events] == ["withdrawn", "reinstated"]
    assert audit_events(c)[-2:] == ["import_withdrawn", "import_reinstated"]


@pytest.mark.parametrize("body,code", [
    ({"reason": "valid reason", "confirm_import_id": "IMP-20990101-0000000000"}, "confirmation_mismatch"),
    ({"reason": "no", "confirm_import_id": None}, "reason_required"),
])
def test_withdraw_requires_reason_and_confirmation(make, body, code):
    c = make()
    iid = do_commit(c)
    body = {**body, "confirm_import_id": body["confirm_import_id"] or iid}
    r = c.post(f"/api/imports/{iid}/withdraw", json=body)
    assert r.status_code == 400 and r.json()["code"] == code
    assert len(record_ids(c, iid)) == 3 and not (imports_dir(c) / iid / "status.json").exists()


def test_invalid_state_transitions(make):
    c = make()
    iid = do_commit(c)
    assert change(c, iid, "reinstate").json()["code"] == "already_active"
    change(c, iid, "withdraw")
    assert change(c, iid, "withdraw").json()["code"] == "already_withdrawn"
    assert change(c, "IMP-20990101-0000000000", "withdraw").status_code == 404
    assert c.post("/api/imports/../withdraw", json={"reason": "x" * 10, "confirm_import_id": "x"}).status_code in (400, 404, 405)


def test_withdrawn_workbook_cannot_be_reimported_and_reinstate_detects_overlap(make):
    c = make()
    data = workbook(GOOD)
    iid = do_commit(c, data=data)
    change(c, iid, "withdraw")
    r = commit(c, data)
    assert r.status_code == 409 and "currently withdrawn" in r.json()["detail"]
    # the same rows arriving in a different workbook file are importable while the first is withdrawn ...
    other = do_commit(c, GOOD + [row(4, "W43-0004.jpg", photo=jpeg_bytes(104))])
    # ... but reinstating the first would then duplicate them, so it is refused
    r = change(c, iid, "reinstate")
    assert r.status_code == 409 and r.json()["code"] == "reinstate_conflict"
    assert record_ids(c, iid) == [] and len(record_ids(c, other)) == 4


def test_withdraw_rolls_back_when_the_reload_fails(make):
    c = make()
    iid = do_commit(c)
    svc = c.app.state.imports
    real, calls = svc._reload, {"n": 0}

    def fail_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("reload broke")
        real()

    svc._reload = fail_once
    r = change(c, iid, "withdraw")
    svc._reload = real
    assert r.status_code == 500 and r.json()["code"] == "state_change_failed"
    assert not (imports_dir(c) / iid / "status.json").exists()           # previous state restored exactly
    assert len(record_ids(c, iid)) == 3 and c.get(f"/api/imports/{iid}").json()["status"] == "completed"
    assert audit_events(c)[-1] == "import_withdraw_failed"


def test_rollback_failure_is_reported_honestly(make, monkeypatch):
    c = make()
    iid = do_commit(c)
    svc = c.app.state.imports
    svc._reload = lambda: (_ for _ in ()).throw(RuntimeError("still broken"))
    r = change(c, iid, "withdraw")
    assert r.status_code == 500 and r.json()["code"] == "rollback_failed"
    assert "Restart the backend" in r.json()["guidance"]


def test_failed_state_write_changes_nothing(make, monkeypatch):
    c = make()
    iid = do_commit(c)
    monkeypatch.setattr(service_mod, "write_state_bytes", lambda f, d: (_ for _ in ()).throw(OSError("disk full")))
    r = change(c, iid, "withdraw")
    assert r.status_code == 500 and r.json()["code"] == "state_write_failed"
    assert len(record_ids(c, iid)) == 3


def test_withdraw_lists_affected_analyses_without_changing_them(make, tmp_path):
    from app.ai.providers import MockConditionProvider

    c = make()
    c.app.state.analysis.provider = MockConditionProvider("stable")
    iid = do_commit(c, [row(1, "W43-0001.jpg", date="10/25/2026", ws="OCT 25 2026", loc="main deck", photo=jpeg_bytes(201))])
    from test_ai_analysis import rid

    earlier = rid(c, "W30-0001")
    later = record_ids(c, iid)[0]
    a = c.post("/api/ai-analysis/compare", json={"earlier_record_id": earlier, "later_record_id": later,
                                                  "acknowledge_warnings": True})
    assert a.status_code == 200, a.text
    aid = a.json()["analysis"]["analysis_id"]
    stored = (tmp_path / "analyses" / f"{aid}.json").read_bytes()
    r = change(c, iid, "withdraw")
    assert r.json()["event"]["affected_analyses"] == [aid]
    assert (tmp_path / "analyses" / f"{aid}.json").read_bytes() == stored   # analysis preserved unchanged
    assert c.get(f"/api/ai-analysis/{aid}").status_code == 200


def test_unreadable_status_file_keeps_records_and_blocks_changes(make):
    c = make()
    iid = do_commit(c)
    (imports_dir(c) / iid / "status.json").write_text("{bad", encoding="utf-8")
    c.app.state.imports._reload()
    assert len(record_ids(c, iid)) == 3
    assert change(c, iid, "withdraw").json()["code"] == "state_unreadable"
    assert any("unreadable status file" in p for p in c.get("/api/imports").json()["problems"])


def test_withdrawal_survives_restart(make, data_root, tmp_path):
    c = make()
    iid = do_commit(c)
    change(c, iid, "withdraw")
    c2 = make()                                    # new app instance over the same folders
    assert record_ids(c2, iid) == [] and c2.get(f"/api/imports/{iid}").json()["status"] == "withdrawn"
