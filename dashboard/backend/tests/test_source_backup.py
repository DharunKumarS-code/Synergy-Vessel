"""Phase 14.1 -- SOURCE backups. Synthetic project trees in temporary folders only; never the real project or the
real backup folder."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from app import backup as bk
from app import source_backup as sb
from app.auth.audit import AuditLog
from app.cli import main as cli_main
from app.config import Settings

SECRET = "SV-TEST-SECRET-VALUE-0123456789"


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def fake_env(backup_id, created):
    return f"ENVIRONMENT for {backup_id} at {created}\nSYNERGY_LOCATION_AI_API_KEY\n"


@pytest.fixture
def proj(tmp_path):
    """A miniature project: allowlisted source plus everything that must never be copied."""
    root = tmp_path / "Synergy Vessel"
    # --- allowlisted source
    write(root / "dashboard/backend/app/main.py", "app = 1\n")
    write(root / "dashboard/backend/app/auth/passwords.py", "def hash_password(): ...\n")   # code, not a secret
    write(root / "dashboard/backend/tests/test_x.py", "def test(): pass\n")
    write(root / "dashboard/backend/requirements.txt", "fastapi>=0.115\n")
    write(root / "dashboard/backend/pytest.ini", "[pytest]\n")
    write(root / "dashboard/frontend/src/App.jsx", "export default 1\n")
    write(root / "dashboard/frontend/public/favicon.svg", "<svg/>")
    write(root / "dashboard/frontend/package.json", "{}")
    write(root / "dashboard/frontend/package-lock.json", "{}")
    write(root / "dashboard/OPERATOR_GUIDE.md", "# guide\n")
    write(root / "automation/src/pipeline.py", "x = 1\n")
    write(root / "automation/run_pipeline.py", "main()\n")
    write(root / "automation/config/settings.yaml", "model: x\n")
    write(root / "automation/tests/fixtures/sample_week.xlsx", b"PK synthetic fixture")      # synthetic: allowed
    write(root / "automation/output/Extraction_Workbook_Hashes.csv", "a,b\n")
    # --- must never be copied
    write(root / "dashboard/backend/.env", f"GROQ_API_KEY={SECRET}")
    write(root / "automation/.env", f"GEMINI_API_KEY={SECRET}")
    write(root / "automation/config/.env.example", "GEMINI_API_KEY=")
    write(root / "automation/config/.env.production", f"KEY={SECRET}")
    write(root / "automation/src/prod.env", f"KEY={SECRET}")
    write(root / "automation/config/credentials.json", f'{{"token": "{SECRET}"}}')
    write(root / "automation/config/server.pem", SECRET)
    write(root / "dashboard/backend/app/__pycache__/main.cpython-311.pyc", b"cache")
    write(root / "dashboard/backend/app/old.pyc", b"cache")
    write(root / "dashboard/backend/.pytest_cache/v/cache", "x")
    write(root / "dashboard/backend/.venv/Lib/site.py", "venv")
    write(root / "dashboard/frontend/node_modules/react/index.js", "module")
    write(root / "dashboard/frontend/dist/assets/index.js", "built")
    write(root / "dashboard/frontend/src/dist/bundle.js", "built")
    write(root / "dashboard/data/auth/users.json", '{"users": []}')
    write(root / "dashboard/data/auth/audit.jsonl", "{}\n")
    write(root / "week 34/Final Official vs AI Location Report.json", "{}")
    write(root / "week 34/Extracted Images - New/IMG_1.jpg", os.urandom(300))
    write(root / "week 34/Nord Kudu Deck Photos.xlsx", b"PK original")
    write(root / "extraction_summary.csv", "a\n")
    write(root / "automation/output/Week 30/Extracted Images/2026-08-02_001.jpg", os.urandom(200))
    write(root / "automation/output/Week 30/Final Official vs AI Location Report.xlsx", b"PK derived")
    write(root / "automation/.state/Week_30.json", "{}")
    write(root / "automation/live_tests/run1/.state/Week_30.json", "{}")
    write(root / "automation/logs/run_1.log", "log")
    write(root / "automation/backups/fix/Week 30/x.xlsx", b"PK")
    write(root / "automation/src/~$lock.xlsx", b"lock")
    write(root / "automation/src/tmp/scratch.py", "x")
    write(root / "notes/personal.txt", "unrelated user file")
    settings = Settings(data_root=root, analysis_dir=root / "dashboard/data/condition_analyses",
                        import_dir=root / "dashboard/data/imports", auth_dir=tmp_path / "auth",
                        backup_dir=tmp_path / "Synergy Vessel Backups")
    return settings, root


def create(proj, **kw):
    settings, root = proj
    return sb.create_source_backup(settings, project_root=root, environment=fake_env, **kw)


def folder(proj, m):
    return sb.source_backup_dir(proj[0]) / m["backup_id"]


EXPECTED = {
    "dashboard/backend/app/main.py", "dashboard/backend/app/auth/passwords.py", "dashboard/backend/tests/test_x.py",
    "dashboard/backend/requirements.txt", "dashboard/backend/pytest.ini", "dashboard/frontend/src/App.jsx",
    "dashboard/frontend/public/favicon.svg", "dashboard/frontend/package.json", "dashboard/frontend/package-lock.json",
    "dashboard/OPERATOR_GUIDE.md", "automation/src/pipeline.py", "automation/run_pipeline.py",
    "automation/config/settings.yaml", "automation/tests/fixtures/sample_week.xlsx",
    "automation/output/Extraction_Workbook_Hashes.csv",
}


# ============================================================================ creation and contents
def test_backup_contains_exactly_the_allowlisted_source(proj):
    m = create(proj, note="first")
    f = folder(proj, m)
    assert sb.SOURCE_ID_RE.match(m["backup_id"]) and f.parent.name == "Source Backups"
    assert {x["path"] for x in m["files"]} == EXPECTED
    on_disk = {p.relative_to(f / "files").as_posix() for p in (f / "files").rglob("*") if p.is_file()}
    assert on_disk == EXPECTED
    assert sorted(p.name for p in f.iterdir()) == ["ENVIRONMENT.txt", "files", "manifest.json"]
    assert m["backup_type"] == "source" and m["project"] == "synergy-vessel" and m["note"] == "first"
    assert m["created_at"].endswith("+00:00")                                         # UTC
    for x in m["files"]:
        assert sb.sha256_file(f / "files" / x["path"]) == x["sha256"] == sb.sha256_file(proj[1] / x["path"])


def test_no_secret_ever_enters_the_backup(proj):
    m = create(proj)
    f = folder(proj, m)
    for p in f.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p
            assert not p.name.startswith(".env") and not p.name.endswith(".env")
    excluded = {e["path"]: e["reason"] for e in m["excluded"]}
    for rel in ("automation/config/.env.example", "automation/config/.env.production", "automation/src/prod.env",
                "automation/config/credentials.json", "automation/config/server.pem"):
        assert "secret" in excluded[rel]
    # .env files in the project folders are outside the allowlist: never even listed
    assert "dashboard/backend/.env" not in excluded and "automation/.env" not in excluded


@pytest.mark.parametrize("rel, words", [
    ("dashboard/backend/app/__pycache__/", "__pycache__"),
    ("dashboard/backend/app/old.pyc", "cache"),
    ("dashboard/frontend/src/dist/", "dist"),
    ("automation/src/~$lock.xlsx", "lock"),
    ("automation/src/tmp/", "tmp"),
    ("automation/output/Week 30/Extracted Images/2026-08-02_001.jpg", "photograph"),
    ("automation/output/Week 30/Final Official vs AI Location Report.xlsx", "Excel"),
    ("automation/live_tests/run1/.state/", ".state"),
    ("automation/logs/run_1.log", "log"),
])
def test_exclusions_inside_allowlisted_folders_are_recorded(proj, rel, words):
    excluded = {e["path"]: e["reason"] for e in create(proj)["excluded"]}
    assert words.lower() in excluded[rel].lower()


@pytest.mark.parametrize("rel", ["dashboard/data/auth/users.json", "week 34/Extracted Images - New/IMG_1.jpg",
                                 "week 34/Nord Kudu Deck Photos.xlsx", "extraction_summary.csv",
                                 "dashboard/backend/.venv/Lib/site.py", "dashboard/frontend/node_modules/react/index.js",
                                 "dashboard/frontend/dist/assets/index.js", "dashboard/backend/.pytest_cache/v/cache",
                                 "automation/.state/Week_30.json", "automation/backups/fix/Week 30/x.xlsx",
                                 "notes/personal.txt", "dashboard/backend/.env"])
def test_paths_outside_the_allowlist_are_never_copied(proj, rel):
    m = create(proj)
    assert rel not in {x["path"] for x in m["files"]}
    assert not (folder(proj, m) / "files" / rel).exists()


@pytest.mark.parametrize("rel, reason", [
    (".env", "secret"), ("dashboard/backend/.env.local", "secret"), ("automation/x/.env", "secret"),
    ("dashboard/data/auth/audit.jsonl", "dashboard application data"), ("week 31/a.json", "historical"),
    ("Week 30/Extracted Images - New/a.jpg", "historical"), ("extraction_summary.csv", "historical"),
    ("dashboard/frontend/node_modules/x.js", "node_modules"), ("dashboard/backend/.venv/x.py", ".venv"),
    ("automation/src/__pycache__/x.pyc", "__pycache__"), ("dashboard/frontend/dist/index.html", "dist"),
    ("automation/src/photo.JPG", "photograph"), ("automation/src/book.xlsx", "Excel"),
    ("automation/tests/fixtures/book.xlsx", None), ("dashboard/backend/app/auth/passwords.py", None),
    ("automation/config/api_key.txt", "secret"), ("automation/config/token.json", "secret"),
])
def test_exclusion_rules(rel, reason):
    got = sb.exclusion_reason(rel)
    assert (got is None) if reason is None else (reason.lower() in got.lower())


def test_environment_report_lists_names_only(proj, monkeypatch):
    monkeypatch.setenv("SYNERGY_LOCATION_AI_API_KEY", SECRET)
    text = sb.environment_report("SRC-20261120-000000-abcdef", "2026-11-20T00:00:00+00:00")
    assert "SYNERGY_LOCATION_AI_API_KEY" in text and SECRET not in text
    assert "Python" in text and "npm ci" in text and "fastapi==" in text.lower()      # installed versions recorded


def test_environment_file_is_hashed_in_the_manifest(proj):
    m = create(proj)
    env = folder(proj, m) / "ENVIRONMENT.txt"
    assert m["environment_file"]["sha256"] == sb.sha256_file(env)


def test_two_backups_never_overwrite_each_other(proj):
    a, b = create(proj), create(proj)
    assert a["backup_id"] != b["backup_id"]
    assert [x["backup_id"] for x in sb.list_source_backups(proj[0])["backups"]] == sorted([a["backup_id"], b["backup_id"]], reverse=True)


def test_duplicate_backup_id_is_refused(proj, monkeypatch):
    monkeypatch.setattr(sb.secrets, "token_hex", lambda n: "abcdef")
    monkeypatch.setattr(sb, "_now", lambda: bk.dt.datetime(2026, 11, 20, 1, 2, 3, tzinfo=bk.dt.timezone.utc))
    first = create(proj)
    before = sorted(p.name for p in sb.source_backup_dir(proj[0]).iterdir())
    with pytest.raises(bk.BackupError, match="already exists"):
        create(proj)
    assert sorted(p.name for p in sb.source_backup_dir(proj[0]).iterdir()) == before
    assert sb.verify_source_backup(proj[0], first["backup_id"]).valid


def test_failed_copy_publishes_nothing(proj, monkeypatch):
    calls = {"n": 0}
    real = sb._copy_hashed

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 5:
            raise OSError("disk error")
        return real(src, dst)

    monkeypatch.setattr(sb, "_copy_hashed", flaky)
    with pytest.raises(OSError):
        create(proj)
    assert list(sb.source_backup_dir(proj[0]).iterdir()) == []                         # no backup, no staging


def test_copy_verification_failure_aborts(proj, monkeypatch):
    monkeypatch.setattr(sb, "sha256_file", lambda p: "0" * 64)
    with pytest.raises(bk.BackupError, match="copy verification failed"):
        create(proj)
    assert list(sb.source_backup_dir(proj[0]).iterdir()) == []


def test_failure_after_manifest_publishes_nothing(proj, monkeypatch):
    monkeypatch.setattr(sb.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError("rename failed")))
    with pytest.raises(OSError):
        create(proj)
    assert list(sb.source_backup_dir(proj[0]).iterdir()) == []


def test_missing_essential_source_is_refused(proj):
    shutil.rmtree(proj[1] / "dashboard/backend/app")
    with pytest.raises(bk.BackupError, match="essential source missing"):
        create(proj)


def test_destination_inside_the_project_is_refused(proj, tmp_path):
    settings, root = proj
    inside = Settings(data_root=root, auth_dir=tmp_path / "auth", backup_dir=root / "backups-here")
    with pytest.raises(bk.BackupError, match="overlaps the project"):
        sb.create_source_backup(inside, project_root=root, environment=fake_env)
    assert not (root / "backups-here").exists()


def test_the_project_files_are_unchanged(proj):
    root = proj[1]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    create(proj)
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


# ============================================================================ verification (read-only)
@pytest.fixture
def made(proj):
    m = create(proj)
    return proj[0], m["backup_id"], folder(proj, m)


def snapshot(f: Path) -> dict:
    return {p.relative_to(f).as_posix(): p.read_bytes() for p in f.rglob("*") if p.is_file()}


def test_verify_valid(made):
    settings, bid, f = made
    r = sb.verify_source_backup(settings, bid)
    assert r.valid and r.files_checked == len(EXPECTED) and r.problems == []


def test_verify_detects_corrupted_file_and_does_not_repair(made):
    settings, bid, f = made
    p = f / "files/automation/src/pipeline.py"
    p.write_bytes(b"y = 2\n")                                                           # same size, other content
    before = snapshot(f)
    r = sb.verify_source_backup(settings, bid)
    assert not r.valid and "content modified (SHA-256 mismatch): automation/src/pipeline.py" in r.problems
    assert snapshot(f) == before                                                      # read-only


def test_verify_detects_size_change_missing_and_unexpected_files(made):
    settings, bid, f = made
    (f / "files/dashboard/OPERATOR_GUIDE.md").write_text("# much longer guide text\n")
    (f / "files/dashboard/backend/app/main.py").unlink()
    write(f / "files/dashboard/backend/app/extra.py", "x")
    write(f / "files/dashboard/backend/.env", f"K={SECRET}")
    write(f / "stray.txt", "x")
    problems = "\n".join(sb.verify_source_backup(settings, bid).problems)
    assert "size mismatch: dashboard/OPERATOR_GUIDE.md" in problems
    assert "missing file: dashboard/backend/app/main.py" in problems
    assert "unexpected file not in the manifest: dashboard/backend/app/extra.py" in problems
    assert "dashboard/backend/.env (exclusion violation" in problems
    assert "unexpected item in the backup folder: stray.txt" in problems


def test_verify_detects_modified_environment_file(made):
    settings, bid, f = made
    (f / "ENVIRONMENT.txt").write_text("changed")
    assert any("ENVIRONMENT.txt" in p for p in sb.verify_source_backup(settings, bid).problems)


def test_verify_detects_tampered_manifest(made):
    settings, bid, f = made
    m = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
    m["files"][0]["sha256"] = "0" * 64
    (f / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    r = sb.verify_source_backup(settings, bid)
    assert not r.valid and "checksum does not match" in r.problems[0]


def test_verify_detects_exclusion_violation_even_with_a_rehashed_manifest(made):
    settings, bid, f = made
    m = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
    data = f"K={SECRET}".encode()
    write(f / "files/automation/config/.env", data)
    m["files"].append({"path": "automation/config/.env", "size": len(data), "sha256": bk.hashlib.sha256(data).hexdigest()})
    m["file_count"] += 1
    m["total_bytes"] += len(data)
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    m["manifest_sha256"] = bk.hashlib.sha256(bk._canonical(body)).hexdigest()
    (f / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    problems = sb.verify_source_backup(settings, bid).problems
    assert any("exclusion violation in manifest: automation/config/.env" in p for p in problems)


@pytest.mark.parametrize("rel", ["../evil.py", "/abs.py", "dashboard/../../x", "C:/x", "dashboard\\x", "week 34/a.json"])
def test_unsafe_manifest_paths_fail_verification(made, rel):
    settings, bid, f = made
    m = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
    m["files"][0]["path"] = rel
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    m["manifest_sha256"] = bk.hashlib.sha256(bk._canonical(body)).hexdigest()
    (f / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    r = sb.verify_source_backup(settings, bid)
    assert not r.valid and any("unsafe path" in p for p in r.problems)


@pytest.mark.parametrize("bad_id", ["../x", "SRC-1", "BKP-20261120-000000-abcdef", "SRC-20261120-000000-ABCDEF"])
def test_invalid_source_backup_ids(made, bad_id):
    r = sb.verify_source_backup(made[0], bad_id)
    assert not r.valid and "invalid source backup id" in r.problems[0]


# ============================================================================ listing and data-backup isolation
def test_listing_reports_incomplete_and_damaged_folders(made):
    settings, bid, f = made
    root = sb.source_backup_dir(settings)
    (root / ".staging-SRC-20261120-000000-aaaaaa").mkdir()
    bad = root / "SRC-20261120-000001-bbbbbb"
    bad.mkdir()
    res = sb.list_source_backups(settings)
    assert [b["backup_id"] for b in res["backups"]] == [bid] and res["backups"][0]["status"] == "manifest OK"
    problems = {u["folder"]: u["problem"] for u in res["unusable"]}
    assert "interrupted" in problems[".staging-SRC-20261120-000000-aaaaaa"] and "missing" in problems[bad.name]


def test_data_backup_listing_ignores_source_backups(made):
    settings = made[0]
    res = bk.list_backups(settings)
    assert res["backups"] == [] and res["unusable"] == []                              # "Source Backups" is not BKP-*
    assert sb.read_manifest is not bk.read_manifest
    with pytest.raises(bk.BackupError, match="unsupported format|does not match"):
        bk.read_manifest(made[2])                                                     # a source manifest is not a data one


def test_cli_source_commands_and_audit(proj, monkeypatch, capsys):
    settings, root = proj
    monkeypatch.setattr(sb, "PROJECT_ROOT", root)
    monkeypatch.setattr(sb, "environment_report", fake_env)
    for k, v in {"SYNERGY_DATA_ROOT": settings.data_root, "SYNERGY_AUTH_DIR": settings.resolved_auth_dir,
                 "SYNERGY_BACKUP_DIR": settings.resolved_backup_dir}.items():
        monkeypatch.setenv(k, str(v))
    assert cli_main(["create-source-backup", "--note", "before release"]) == 0
    out = capsys.readouterr().out
    assert "created and verified" in out and "Copy both to another physical drive" in out
    bid = sb.list_source_backups(settings)["backups"][0]["backup_id"]
    assert cli_main(["list-source-backups"]) == 0 and bid in capsys.readouterr().out
    assert cli_main(["verify-source-backup", bid]) == 0 and "VALID" in capsys.readouterr().out
    assert cli_main(["verify-source-backup", "SRC-20261120-000000-ffffff"]) == 1
    assert cli_main(["list-backups"]) == 0 and "(none)" in capsys.readouterr().out    # data listing unaffected
    events = [e["event"] for e in reversed(AuditLog(settings.resolved_auth_dir).entries())]
    assert events[-3:] == ["source_backup_created", "source_backup_verified", "source_backup_verified"]
    assert AuditLog(settings.resolved_auth_dir).verify().valid
