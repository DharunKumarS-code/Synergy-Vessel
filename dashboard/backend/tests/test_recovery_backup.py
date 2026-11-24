"""Phase 14.4.1 -- RECOVERY-ARTEFACT backups. Synthetic project trees in temporary folders only; never the real
automation/backups, automation/.state or the real backup folder."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from app import backup as bk
from app import recovery_backup as rb
from app import source_backup as sb
from app.auth.audit import AuditLog
from app.cli import main as cli_main
from app.config import Settings

SECRET = "SV-TEST-RECOVERY-SECRET-987654321"


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


EXPECTED = {
    "automation/backups/AE8_pre_live_20261112_165912/Week 30/Final Official vs AI Location Report.xlsx",
    "automation/backups/AE8_pre_live_20261112_165912/Week 30/manifest.csv",
    "automation/backups/AE8_pre_live_20261112_165912/Week 30/state_Week_30.json",
    "automation/backups/official_location_fix_20261116/Week 30/Week_30_Image_Register.xlsx",
    "automation/backups/official_location_fix_20261116/week 31/Week_31_Image_Register.xlsx",
    "automation/backups/snap/Extracted Images/a.jpg",
    "automation/.state/Week_30.json",
}


@pytest.fixture
def proj(tmp_path):
    root = tmp_path / "Synergy Vessel"
    for rel in EXPECTED:
        write(root / rel, os.urandom(300) if rel.endswith((".xlsx", ".jpg")) else f"data for {rel}\n")
    # inside the allowlisted folders but must be excluded
    write(root / "automation/backups/.env", f"KEY={SECRET}")
    write(root / "automation/backups/snap/.env.local", f"KEY={SECRET}")
    write(root / "automation/backups/snap/credentials.json", f'{{"k": "{SECRET}"}}')
    write(root / "automation/backups/snap/server.key", SECRET)
    write(root / "automation/.state/api_token.json", f'{{"t": "{SECRET}"}}')
    write(root / "automation/backups/snap/run.log", "log")
    write(root / "automation/backups/snap/~$Week_30_Image_Register.xlsx", b"lock")
    write(root / "automation/backups/snap/x.tmp", "tmp")
    write(root / "automation/backups/snap/__pycache__/m.pyc", b"cache")
    write(root / "automation/backups/snap/tool.py", "print(1)")                       # not a recovery file type
    write(root / "automation/backups/snap/notes.docx", b"doc")
    write(root / "automation/backups/.staging-x/partial.xlsx", b"partial")
    # outside the allowlist: never read
    write(root / "automation/.env", f"GEMINI_API_KEY={SECRET}")
    write(root / "automation/src/pipeline.py", "x = 1")
    write(root / "automation/output/Week 30/report.xlsx", b"PK")
    write(root / "dashboard/data/auth/users.json", "{}")
    write(root / "dashboard/backend/app/main.py", "app = 1")
    write(root / "dashboard/frontend/node_modules/x.js", "x")
    write(root / "dashboard/backend/.venv/site.py", "x")
    write(root / "week 34/Extracted Images - New/IMG.jpg", os.urandom(100))
    write(root / "week 34/Nord Kudu Deck Photos.xlsx", b"PK original")
    settings = Settings(data_root=root, analysis_dir=root / "dashboard/data/condition_analyses",
                        import_dir=root / "dashboard/data/imports", auth_dir=tmp_path / "auth",
                        backup_dir=tmp_path / "Synergy Vessel Backups")
    return settings, root


def create(proj, **kw):
    return rb.create_recovery_backup(proj[0], project_root=proj[1], **kw)


def folder(proj, m):
    return rb.recovery_backup_dir(proj[0]) / m["backup_id"]


def rehash_manifest(path: Path, m: dict) -> None:
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    m["manifest_sha256"] = hashlib.sha256(bk._canonical(body)).hexdigest()
    path.write_text(json.dumps(m), encoding="utf-8")


# ============================================================================ creation
def test_backup_contains_exactly_the_allowlisted_artefacts_with_relative_paths(proj):
    m = create(proj, note="pre-fix snapshots")
    f = folder(proj, m)
    assert rb.RECOVERY_ID_RE.match(m["backup_id"]) and f.parent.name == "Recovery Artefacts"
    assert {x["path"] for x in m["files"]} == EXPECTED
    assert {p.relative_to(f / "files").as_posix() for p in (f / "files").rglob("*") if p.is_file()} == EXPECTED
    assert sorted(p.name for p in f.iterdir()) == ["files", "manifest.json"]
    assert m["format_version"] == "recovery-1" and m["backup_type"] == "recovery" and m["note"] == "pre-fix snapshots"
    assert m["created_at"].endswith("+00:00") and set(m["source_paths"]) == set(rb.SECTIONS)
    assert m["file_count"] == len(EXPECTED) and m["total_bytes"] == sum(x["size"] for x in m["files"])


def test_sha256_matches_source_and_copy(proj):
    m = create(proj)
    f = folder(proj, m)
    for x in m["files"]:
        assert x["sha256"] == hashlib.sha256((proj[1] / x["path"]).read_bytes()).hexdigest() == rb.sha256_file(f / "files" / x["path"])


def test_manifest_checksum_is_valid(proj):
    m = create(proj)
    stored = json.loads((folder(proj, m) / "manifest.json").read_text(encoding="utf-8"))
    body = {k: v for k, v in stored.items() if k != "manifest_sha256"}
    assert stored["manifest_sha256"] == hashlib.sha256(bk._canonical(body)).hexdigest() == m["manifest_sha256"]


def test_no_secret_ever_enters_the_backup(proj):
    m = create(proj)
    for p in folder(proj, m).rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p
    excluded = {e["path"]: e["reason"] for e in m["excluded"]}
    for rel in ("automation/backups/.env", "automation/backups/snap/.env.local", "automation/backups/snap/credentials.json",
                "automation/backups/snap/server.key", "automation/.state/api_token.json"):
        assert "secret" in excluded[rel]
    assert "automation/.env" not in excluded                     # outside the allowlist: never even listed


@pytest.mark.parametrize("rel, words", [
    ("automation/backups/snap/run.log", "log"),
    ("automation/backups/snap/~$Week_30_Image_Register.xlsx", "lock"),
    ("automation/backups/snap/x.tmp", "temporary"),
    ("automation/backups/snap/__pycache__/", "__pycache__"),
    ("automation/backups/snap/tool.py", "not a recovery file type"),
    ("automation/backups/snap/notes.docx", "not a recovery file type"),
    ("automation/backups/.staging-x/", ".staging-x"),
])
def test_exclusions_are_recorded_with_reasons(proj, rel, words):
    excluded = {e["path"]: e["reason"] for e in create(proj)["excluded"]}
    assert words.lower() in excluded[rel].lower()


@pytest.mark.parametrize("rel", ["automation/src/pipeline.py", "automation/output/Week 30/report.xlsx",
                                 "dashboard/data/auth/users.json", "dashboard/backend/app/main.py",
                                 "dashboard/frontend/node_modules/x.js", "dashboard/backend/.venv/site.py",
                                 "week 34/Extracted Images - New/IMG.jpg", "week 34/Nord Kudu Deck Photos.xlsx",
                                 "automation/.env"])
def test_paths_outside_the_allowlist_are_never_copied(proj, rel):
    m = create(proj)
    assert rel not in {x["path"] for x in m["files"]} and not (folder(proj, m) / "files" / rel).exists()


@pytest.mark.parametrize("rel, ok", [
    ("automation/backups/a/b.xlsx", True), ("automation/.state/Week_31.json", True),
    ("automation/backups/a/photo.JPG", True), ("automation/src/x.py", False), ("automation/backupsx/a.json", False),
    ("automation/backups/a/.env", False), ("automation/backups/a/prod.env", False), ("automation/backups/a/id_rsa", False),
    ("automation/backups/a/b.pem", False), ("automation/backups/a/b.log", False), ("dashboard/data/auth/users.json", False),
])
def test_exclusion_rules(rel, ok):
    assert (rb.exclusion_reason(rel) is None) is ok


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="no symlink support")
def test_symlinks_are_excluded_not_followed(proj, tmp_path):
    outside = write(tmp_path / "outside" / "secret.xlsx", b"outside data")
    try:
        os.symlink(outside, proj[1] / "automation/backups/link.xlsx")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    m = create(proj)
    assert {e["path"]: e["reason"] for e in m["excluded"]}["automation/backups/link.xlsx"].startswith("symbolic link")
    assert "automation/backups/link.xlsx" not in {x["path"] for x in m["files"]}


def test_missing_state_folder_is_recorded(proj):
    shutil.rmtree(proj[1] / "automation/.state")
    m = create(proj)
    assert m["missing_allowlisted"] == ["automation/.state/"]
    assert not any(x["path"].startswith("automation/.state/") for x in m["files"])


def test_missing_backups_folder_is_refused(proj):
    shutil.rmtree(proj[1] / "automation/backups")
    with pytest.raises(bk.BackupError, match="recovery folder missing"):
        create(proj)
    assert not rb.recovery_backup_dir(proj[0]).exists()


def test_empty_allowlist_is_refused(proj):
    for d in ("automation/backups", "automation/.state"):
        shutil.rmtree(proj[1] / d)
    (proj[1] / "automation/backups").mkdir()
    with pytest.raises(bk.BackupError, match="no recovery artefacts"):
        create(proj)


def test_two_backups_never_overwrite_each_other(proj):
    a, b = create(proj), create(proj)
    assert a["backup_id"] != b["backup_id"]
    assert {x["backup_id"] for x in rb.list_recovery_backups(proj[0])["backups"]} == {a["backup_id"], b["backup_id"]}


def test_duplicate_backup_id_is_refused(proj, monkeypatch):
    monkeypatch.setattr(rb.secrets, "token_hex", lambda n: "abcdef")
    monkeypatch.setattr(rb, "_now", lambda: bk.dt.datetime(2026, 11, 20, 1, 2, 3, tzinfo=bk.dt.timezone.utc))
    first = create(proj)
    before = sorted(p.name for p in rb.recovery_backup_dir(proj[0]).iterdir())
    with pytest.raises(bk.BackupError, match="already exists"):
        create(proj)
    assert sorted(p.name for p in rb.recovery_backup_dir(proj[0]).iterdir()) == before
    assert rb.verify_recovery_backup(proj[0], first["backup_id"]).valid


def test_failed_copy_cleans_staging_and_publishes_nothing(proj, monkeypatch):
    calls = {"n": 0}
    real = rb._copy_hashed

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk error")
        return real(src, dst)

    monkeypatch.setattr(rb, "_copy_hashed", flaky)
    with pytest.raises(OSError):
        create(proj)
    assert list(rb.recovery_backup_dir(proj[0]).iterdir()) == []


def test_failed_hash_check_aborts(proj, monkeypatch):
    monkeypatch.setattr(rb, "sha256_file", lambda p: "0" * 64)
    with pytest.raises(bk.BackupError, match="copy verification failed"):
        create(proj)
    assert list(rb.recovery_backup_dir(proj[0]).iterdir()) == []


def test_failed_publish_cleans_staging(proj, monkeypatch):
    monkeypatch.setattr(rb.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError("rename failed")))
    with pytest.raises(OSError):
        create(proj)
    assert list(rb.recovery_backup_dir(proj[0]).iterdir()) == []


def test_destination_inside_the_project_is_refused(proj, tmp_path):
    inside = Settings(data_root=proj[1], auth_dir=tmp_path / "auth", backup_dir=proj[1] / "automation" / "rb")
    with pytest.raises(bk.BackupError, match="overlaps the project"):
        rb.create_recovery_backup(inside, project_root=proj[1])
    assert not (proj[1] / "automation" / "rb").exists()


def test_source_files_are_unchanged(proj):
    before = {p: p.read_bytes() for p in proj[1].rglob("*") if p.is_file()}
    create(proj)
    assert {p: p.read_bytes() for p in proj[1].rglob("*") if p.is_file()} == before


# ============================================================================ verification (read-only)
@pytest.fixture
def made(proj):
    m = create(proj)
    return proj[0], m["backup_id"], folder(proj, m)


def snapshot(f: Path) -> dict:
    return {p.relative_to(f).as_posix(): p.read_bytes() for p in f.rglob("*") if p.is_file()}


def test_verify_valid(made):
    r = rb.verify_recovery_backup(made[0], made[1])
    assert r.valid and r.files_checked == len(EXPECTED) and r.problems == []


def test_verify_detects_corruption_without_repairing(made):
    settings, bid, f = made
    p = f / "files/automation/.state/Week_30.json"
    p.write_bytes(bytes(b ^ 1 for b in p.read_bytes()))                                 # same size, other content
    before = snapshot(f)
    r = rb.verify_recovery_backup(settings, bid)
    assert not r.valid and "content modified (SHA-256 mismatch): automation/.state/Week_30.json" in r.problems
    assert snapshot(f) == before


def test_verify_detects_size_missing_and_unexpected(made):
    settings, bid, f = made
    (f / "files/automation/backups/AE8_pre_live_20261112_165912/Week 30/manifest.csv").write_text("much longer content here\n")
    (f / "files/automation/backups/snap/Extracted Images/a.jpg").unlink()
    write(f / "files/automation/backups/extra.json", "{}")
    write(f / "files/automation/backups/.env", f"K={SECRET}")
    write(f / "stray.txt", "x")
    problems = "\n".join(rb.verify_recovery_backup(settings, bid).problems)
    assert "size mismatch: automation/backups/AE8_pre_live_20261112_165912/Week 30/manifest.csv" in problems
    assert "missing file: automation/backups/snap/Extracted Images/a.jpg" in problems
    assert "unexpected file not in the manifest: automation/backups/extra.json" in problems
    assert "automation/backups/.env (forbidden: secret" in problems
    assert "unexpected item in the backup folder: stray.txt" in problems


def test_verify_detects_modified_manifest(made):
    settings, bid, f = made
    m = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
    m["files"][0]["size"] += 1
    (f / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    r = rb.verify_recovery_backup(settings, bid)
    assert not r.valid and "checksum does not match" in r.problems[0]


def test_verify_detects_inconsistent_counts_even_with_rehashed_manifest(made):
    settings, bid, f = made
    m = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
    m["file_count"] += 1
    rehash_manifest(f / "manifest.json", m)
    assert "file count is inconsistent" in rb.verify_recovery_backup(settings, bid).problems[0]


def test_verify_detects_forbidden_file_injected_with_rehashed_manifest(made):
    settings, bid, f = made
    m = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
    data = f"K={SECRET}".encode()
    write(f / "files/automation/.state/.env", data)
    m["files"].append({"path": "automation/.state/.env", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    m["file_count"] += 1
    m["total_bytes"] += len(data)
    rehash_manifest(f / "manifest.json", m)
    problems = rb.verify_recovery_backup(settings, bid).problems
    assert any("forbidden file in manifest: automation/.state/.env" in p for p in problems)


@pytest.mark.parametrize("rel", ["../evil.json", "/abs.json", "automation/backups/../../x.json", "C:/x.json",
                                 "automation\\backups\\x.json", "automation/src/x.json", "week 34/a.json", 7])
def test_unsafe_manifest_paths_fail_verification(made, rel):
    settings, bid, f = made
    m = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
    m["files"][0]["path"] = rel
    rehash_manifest(f / "manifest.json", m)
    r = rb.verify_recovery_backup(settings, bid)
    assert not r.valid and any("unsafe path" in p for p in r.problems)


@pytest.mark.parametrize("bad_id", ["../x", "RA-1", "SRC-20261120-000000-abcdef", "BKP-20261120-000000-abcdef",
                                    "RA-20261120-000000-ABCDEF"])
def test_invalid_recovery_backup_ids(made, bad_id):
    r = rb.verify_recovery_backup(made[0], bad_id)
    assert not r.valid and "invalid recovery backup id" in r.problems[0]


# ============================================================================ listing, isolation, CLI
def test_listing_reports_incomplete_and_damaged_folders(made):
    settings, bid, f = made
    root = rb.recovery_backup_dir(settings)
    (root / ".staging-RA-20261120-000000-aaaaaa").mkdir()
    (root / "RA-20261120-000001-bbbbbb").mkdir()
    res = rb.list_recovery_backups(settings)
    assert [b["backup_id"] for b in res["backups"]] == [bid] and res["backups"][0]["status"] == "manifest OK"
    problems = {u["folder"]: u["problem"] for u in res["unusable"]}
    assert "interrupted" in problems[".staging-RA-20261120-000000-aaaaaa"]
    assert "missing" in problems["RA-20261120-000001-bbbbbb"]


def test_isolation_from_data_and_source_backups(made):
    settings, bid, f = made
    assert bk.list_backups(settings) == {"backup_dir": str(settings.resolved_backup_dir), "backups": [], "unusable": []}
    assert sb.list_source_backups(settings)["backups"] == []
    with pytest.raises(bk.BackupError, match="unsupported format"):
        bk.read_manifest(f)                                                              # data tool rejects it
    with pytest.raises(bk.BackupError, match="not a source-backup manifest"):
        sb.read_manifest(f)                                                              # source tool rejects it
    assert not bk.verify_backup(settings, bid).valid and not sb.verify_source_backup(settings, bid).valid


def test_recovery_tool_rejects_source_manifests(proj, tmp_path):
    fake = rb.recovery_backup_dir(proj[0]) / "RA-20261120-000000-abcdef"
    write(fake / "manifest.json", json.dumps({"format_version": "source-1", "backup_type": "source"}))
    r = rb.verify_recovery_backup(proj[0], "RA-20261120-000000-abcdef")
    assert not r.valid and "not a recovery-backup manifest" in r.problems[0]


def test_cli_recovery_commands_and_audit(proj, monkeypatch, capsys):
    settings, root = proj
    monkeypatch.setattr(rb, "PROJECT_ROOT", root)
    for k, v in {"SYNERGY_DATA_ROOT": settings.data_root, "SYNERGY_AUTH_DIR": settings.resolved_auth_dir,
                 "SYNERGY_BACKUP_DIR": settings.resolved_backup_dir}.items():
        monkeypatch.setenv(k, str(v))
    assert cli_main(["create-recovery-backup", "--note", "pre-fix"]) == 0
    out = capsys.readouterr().out
    assert "created and verified" in out and "another physical drive" in out
    bid = rb.list_recovery_backups(settings)["backups"][0]["backup_id"]
    assert cli_main(["list-recovery-backups"]) == 0 and bid in capsys.readouterr().out
    assert cli_main(["verify-recovery-backup", bid]) == 0 and "VALID" in capsys.readouterr().out
    (rb.recovery_backup_dir(settings) / bid / "files/automation/.state/Week_30.json").write_text("tampered")
    assert cli_main(["verify-recovery-backup", bid]) == 1 and "INVALID" in capsys.readouterr().out
    shutil.rmtree(root / "automation/backups")
    assert cli_main(["create-recovery-backup"]) == 1 and "Error:" in capsys.readouterr().out
    assert cli_main(["list-backups"]) == 0 and "(none)" in capsys.readouterr().out          # data listing unaffected
    assert cli_main(["list-source-backups"]) == 0 and "(none)" in capsys.readouterr().out   # source listing unaffected
    events = [e["event"] for e in reversed(AuditLog(settings.resolved_auth_dir).entries())]
    assert events[-4:] == ["recovery_backup_created", "recovery_backup_verified", "recovery_backup_verified",
                           "recovery_backup_failed"]
    assert AuditLog(settings.resolved_auth_dir).verify().valid
