"""Phase 14.4.4 -- verification reports (verify-* --report). Temporary folders only."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import pytest

from app import backup as bk
from app import backup_reports as br
from app import recovery_backup as rb
from app import source_backup as sb
from app.backup import VerifyReport
from app.cli import main as cli_main
from app.config import Settings

SECRET = "SV-REPORT-TEST-SECRET-555"


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """One synthetic project holding data (week folder), source and recovery artefacts, plus one backup of each."""
    root = tmp_path / "Synergy Vessel"
    write(root / "week 34/Final Official vs AI Location Report.json", "{}")
    write(root / "week 34/Extracted Images - New/IMG_1.jpg", os.urandom(400))
    write(root / "dashboard/backend/app/main.py", "app = 1\n")
    write(root / "dashboard/frontend/src/App.jsx", "x\n")
    write(root / "dashboard/frontend/package.json", "{}")
    write(root / "automation/src/p.py", "x\n")
    write(root / "automation/backups/fix/week 34/Week_34_Image_Register.xlsx", os.urandom(300))
    write(root / "automation/.env", f"KEY={SECRET}")
    s = Settings(data_root=root, analysis_dir=tmp_path / "app/analyses", import_dir=tmp_path / "app/imports",
                 auth_dir=tmp_path / "app/auth", backup_dir=tmp_path / "Synergy Vessel Backups")
    for k, v in {"SYNERGY_DATA_ROOT": s.data_root, "SYNERGY_ANALYSIS_DIR": s.analysis_dir, "SYNERGY_IMPORT_DIR": s.import_dir,
                 "SYNERGY_AUTH_DIR": s.resolved_auth_dir, "SYNERGY_BACKUP_DIR": s.resolved_backup_dir}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("SYNERGY_LOCATION_AI_API_KEY", SECRET)
    ids = {
        "data": bk.create_backup(s)["backup_id"],
        "source": sb.create_source_backup(s, project_root=root, environment=lambda i, c: f"env {i}\n")["backup_id"],
        "recovery": rb.create_recovery_backup(s, project_root=root)["backup_id"],
    }
    folders = {"data": s.resolved_backup_dir / ids["data"], "source": sb.source_backup_dir(s) / ids["source"],
               "recovery": rb.recovery_backup_dir(s) / ids["recovery"]}
    return s, ids, folders


COMMANDS = {"data": "verify-backup", "source": "verify-source-backup", "recovery": "verify-recovery-backup"}


def tree(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}


def reports(s: Settings) -> list[Path]:
    d = br.reports_dir(s)
    return sorted(d.iterdir()) if d.is_dir() else []


@pytest.mark.parametrize("kind", ["data", "source", "recovery"])
def test_valid_report_is_saved_with_the_right_name_and_content(env, kind, capsys):
    s, ids, folders = env
    before = tree(folders[kind])
    assert cli_main([COMMANDS[kind], ids[kind], "--report"]) == 0
    out = capsys.readouterr().out
    [rep] = reports(s)
    assert re.fullmatch(rf"{br.KINDS[kind]}_{ids[kind]}_\d{{8}}T\d{{6}}Z\.txt", rep.name)
    assert rep.parent == s.resolved_backup_dir / "Backup Reports" and f"Verification report saved: {rep}" in out
    text = rep.read_text(encoding="utf-8")
    for line in ("Result            : VALID", "Exit status       : 0", f"Backup ID         : {ids[kind]}",
                 f"Backup type       : {br.KINDS[kind]}", "Manifest status   : OK", "Missing files     : 0",
                 "Unexpected files  : 0", "Hash mismatches   : 0", "Problems: none", f"--report",
                 "NOT a backup copy", f"Backup location   : {folders[kind]}"):
        assert line in text, line
    assert re.search(r"Files checked     : [1-9]", text) and re.search(r"Backup size       : [\d,]+ bytes", text)
    assert SECRET not in text
    assert tree(folders[kind]) == before                                              # backup untouched


@pytest.mark.parametrize("kind", ["data", "source", "recovery"])
def test_invalid_verification_is_recorded_as_invalid(env, kind, capsys):
    s, ids, folders = env
    files = sorted(p for p in (folders[kind] / "files").rglob("*") if p.is_file())
    files[0].write_bytes(bytes(b ^ 1 for b in files[0].read_bytes()))                  # same size, other content
    write(folders[kind] / "files" / "dashboard" / "extra.py" if kind == "source" else
          folders[kind] / "files" / ("historical/extra.json" if kind == "data" else "automation/backups/extra.json"), "{}")
    before = tree(folders[kind])
    assert cli_main([COMMANDS[kind], ids[kind], "--report"]) == 1
    [rep] = reports(s)
    text = rep.read_text(encoding="utf-8")
    assert text.startswith("Synergy Vessel backup verification report -- INVALID")
    for line in ("Result            : INVALID", "Exit status       : 1", "Hash mismatches   : 1", "Unexpected files  : 1"):
        assert line in text, line
    assert "content modified (SHA-256 mismatch)" in text and "VALID\n" not in text.split("Result")[1][:20]
    assert tree(folders[kind]) == before                                              # verification never repairs


def test_missing_file_and_bad_manifest_are_reported(env):
    s, ids, folders = env
    next(p for p in (folders["recovery"] / "files").rglob("*") if p.is_file()).unlink()
    assert cli_main(["verify-recovery-backup", ids["recovery"], "--report"]) == 1
    assert "Missing files     : 1" in reports(s)[0].read_text(encoding="utf-8")
    m = json.loads((folders["source"] / "manifest.json").read_text(encoding="utf-8"))
    m["note"] = "edited"
    (folders["source"] / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    assert cli_main(["verify-source-backup", ids["source"], "--report"]) == 1
    text = next(p for p in reports(s) if p.name.startswith("SOURCE_")).read_text(encoding="utf-8")
    assert "Manifest status   : INVALID (manifest.json was modified" in text and "Result            : INVALID" in text


def test_unknown_or_unsafe_id_is_reported_as_invalid(env):
    s, ids, folders = env
    assert cli_main(["verify-backup", "../../etc", "--report"]) == 1
    [rep] = reports(s)
    assert rep.name.startswith("DATA_INVALID-ID_") and rep.parent == br.reports_dir(s)
    text = rep.read_text(encoding="utf-8")
    assert "Result            : INVALID" in text and "Manifest status   : NOT AVAILABLE" in text


def test_without_report_flag_nothing_is_written(env):
    s, ids, _ = env
    assert cli_main(["verify-backup", ids["data"]]) == 0
    assert not br.reports_dir(s).exists()


def test_reports_never_overwrite_and_leave_no_temp_files(env):
    s, ids, _ = env
    now = dt.datetime(2026, 11, 20, 6, 0, 0, tzinfo=dt.timezone.utc)
    r = sb.verify_source_backup(s, ids["source"])
    a = br.write_report(s, "source", ids["source"], r, command="c", exit_status=0, now=now)
    b = br.write_report(s, "source", ids["source"], r, command="c", exit_status=0, now=now)
    assert a.name == f"SOURCE_{ids['source']}_20261120T060000Z.txt" and b.name == a.name.replace(".txt", "_2.txt")
    assert a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")
    assert [p.name for p in reports(s)] == sorted([a.name, b.name])                 # no .tmp left


def test_failed_write_leaves_nothing_behind(env, monkeypatch):
    s, ids, _ = env
    monkeypatch.setattr(br.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        br.write_report(s, "data", ids["data"], bk.verify_backup(s, ids["data"]), command="c", exit_status=0)
    assert reports(s) == []


def test_a_false_success_can_never_be_recorded(env):
    s, ids, _ = env
    with pytest.raises(ValueError, match="does not match"):
        br.write_report(s, "data", ids["data"], VerifyReport(ids["data"], False, 0, ["x"]), command="c", exit_status=0)
    with pytest.raises(ValueError):
        br.write_report(s, "data", ids["data"], VerifyReport(ids["data"], True, 1, []), command="c", exit_status=1)
    assert reports(s) == []


@pytest.mark.parametrize("valid_backup, expected", [(True, 3), (False, 1)])
def test_report_failure_is_never_silent(env, monkeypatch, capsys, valid_backup, expected):
    s, ids, folders = env
    if not valid_backup:
        next(p for p in (folders["data"] / "files").rglob("*") if p.is_file()).write_bytes(b"changed")
    monkeypatch.setattr(br, "write_report", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert cli_main(["verify-backup", ids["data"], "--report"]) == expected
    assert "report was NOT saved" in capsys.readouterr().out


def test_reports_folder_does_not_disturb_backup_listings(env):
    s, ids, _ = env
    assert cli_main(["verify-backup", ids["data"], "--report"]) == 0
    res = bk.list_backups(s)
    assert [b["backup_id"] for b in res["backups"]] == [ids["data"]] and res["unusable"] == []
    assert len(sb.list_source_backups(s)["backups"]) == 1 and len(rb.list_recovery_backups(s)["backups"]) == 1


def test_long_problem_lists_are_truncated(env):
    r = VerifyReport("BKP-20261120-000000-abcdef", False, 0, [f"missing file: f{i}" for i in range(1200)])
    text = br.render("data", r.backup_id, r, location="x", manifest_status="OK", size_bytes=1, command="c",
                     exit_status=1, verified_at=dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc))
    assert "Missing files     : 1200" in text and "... and 200 more" in text
