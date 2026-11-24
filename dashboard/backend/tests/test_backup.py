"""Phase 8.3 -- backup creation, verification, listing and safe restore. Isolated temp folders only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from app import backup as bk
from app.auth.audit import AuditLog
from app.cli import main as cli_main
from app.config import Settings


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


@pytest.fixture
def env(tmp_path):
    """A miniature production layout: 2 week folders, a root data file, dashboard app data, plus things that
    must be excluded (.env, temp/lock files, caches, import staging)."""
    root = tmp_path / "prod"
    for w in ("Week 30", "week 31"):
        write(root / w / "Final Official vs AI Location Report.json", json.dumps({"week": w}))
        write(root / w / "Week_30_Image_Register.xlsx", b"PK\x03\x04 register " + w.encode())
        write(root / w / "Extracted Images - New" / "W30-0001.jpg", os.urandom(5000))
        write(root / w / "~$Week_30_Image_Register.xlsx", b"excel lock file")
    write(root / "extraction_summary.csv", "Week,File\nWeek 30,a.jpg\n")
    write(root / ".env", "GEMINI_API_KEY=should-never-be-copied")
    write(root / "automation" / "config" / "settings.yaml", "model: x")       # not a week folder -> not included
    write(root / "automation" / ".env", "SECRET=1")
    app = tmp_path / "appdata"
    write(app / "analyses" / "CA-20261119-0000000001.json", '{"analysis_id": "CA-20261119-0000000001"}')
    write(app / "analyses" / ".CA-x.json.tmp", "partial")
    write(app / "imports" / "IMP-20261119-0123456789" / "manifest.json", "{}")
    write(app / "imports" / "IMP-20261119-0123456789" / "source.xlsx", b"PK workbook")
    write(app / "imports" / ".staging-IMP-20261119-abcdefabcd" / "source.xlsx", b"interrupted")
    write(app / "imports" / "__pycache__" / "x.pyc", b"cache")
    AuditLog(app / "auth").record("user_created", actor={"user_id": "cli"}, target="USR-1")
    write(app / "auth" / "users.json", json.dumps({"version": 1, "users": []}))
    s = Settings(data_root=root, analysis_dir=app / "analyses", import_dir=app / "imports", auth_dir=app / "auth",
                 backup_dir=tmp_path / "backups")
    return s


def tree_hash(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


# ============================================================================ creation
def test_backup_contents_manifest_and_hashes(env, tmp_path):
    before = tree_hash(tmp_path / "prod"), tree_hash(tmp_path / "appdata")
    m = bk.create_backup(env, note="before upgrade")
    assert (tree_hash(tmp_path / "prod"), tree_hash(tmp_path / "appdata")) == before   # sources untouched
    paths = {f["path"] for f in m["files"]}
    assert {"historical/Week 30/Extracted Images - New/W30-0001.jpg", "historical/week 31/Week_30_Image_Register.xlsx",
            "historical/extraction_summary.csv", "app/condition_analyses/CA-20261119-0000000001.json",
            "app/imports/IMP-20261119-0123456789/source.xlsx", "app/auth/users.json",
            "app/auth/audit.jsonl", "app/auth/audit.head.json"} <= paths
    joined = "\n".join(paths)
    for banned in (".env", "~$", ".tmp", ".staging-", "__pycache__", "automation"):
        assert banned not in joined
    excluded = {e["path"] for e in m["excluded"]}
    assert "app/imports/.staging-IMP-20261119-abcdefabcd/" in excluded and "historical/Week 30/~$Week_30_Image_Register.xlsx" in excluded
    assert m["file_count"] == len(m["files"]) and m["total_bytes"] == sum(f["size"] for f in m["files"])
    assert m["app_version"] and m["created_at"] and m["note"] == "before upgrade"
    assert {s["section"] for s in m["sources"]} >= {"historical/Week 30", "app/auth"}
    folder = env.resolved_backup_dir / m["backup_id"]
    for f in m["files"]:
        copy = folder / "files" / f["path"]
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == f["sha256"] and copy.stat().st_size == f["size"]
    secret_free = (folder / "manifest.json").read_text(encoding="utf-8")
    assert "should-never-be-copied" not in secret_free and "SECRET=1" not in secret_free
    assert not any(p.name.startswith(".staging-") for p in env.resolved_backup_dir.iterdir())


def test_two_backups_never_overwrite_each_other(env):
    a, b = bk.create_backup(env), bk.create_backup(env)
    assert a["backup_id"] != b["backup_id"] and len(bk.list_backups(env)["backups"]) == 2


def test_backup_dir_inside_a_source_is_refused(env):
    from dataclasses import replace

    with pytest.raises(bk.BackupError, match="overlaps"):
        bk.create_backup(replace(env, backup_dir=env.import_dir / "backups"))


def test_failed_copy_leaves_no_backup(env, monkeypatch):
    calls = {"n": 0}
    real = bk._copy_hashed

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(bk, "_copy_hashed", flaky)
    with pytest.raises(OSError):
        bk.create_backup(env)
    assert list(env.resolved_backup_dir.iterdir()) == []   # staging removed, nothing listed


def test_copy_verification_failure_aborts(env, monkeypatch):
    monkeypatch.setattr(bk, "sha256_file", lambda p: "0" * 64)
    with pytest.raises(bk.BackupError, match="copy verification failed"):
        bk.create_backup(env)
    assert list(env.resolved_backup_dir.iterdir()) == []


def test_symlinks_are_not_silently_skipped(env):
    target = env.data_root / "Week 30" / "link.jpg"
    try:
        os.symlink(env.data_root / "extraction_summary.csv", target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(bk.BackupError, match="symbolic link"):
        bk.create_backup(env)


def test_missing_data_root_or_weeks_is_an_error(env, tmp_path):
    from dataclasses import replace

    with pytest.raises(bk.BackupError, match="data root not found"):
        bk.create_backup(replace(env, data_root=tmp_path / "nope"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(bk.BackupError, match="no historical week folders"):
        bk.create_backup(replace(env, data_root=tmp_path / "empty"))


# ============================================================================ list / show / verify
def test_list_show_and_verify_valid(env):
    m = bk.create_backup(env)
    listing = bk.list_backups(env)
    assert [b["backup_id"] for b in listing["backups"]] == [m["backup_id"]] and listing["unusable"] == []
    info = bk.show_backup(env, m["backup_id"])
    assert info["sections"]["historical/Week 30"]["files"] == 3 and "files" not in info
    r = bk.verify_backup(env, m["backup_id"])
    assert r.valid and r.files_checked == m["file_count"] and r.problems == []


@pytest.fixture
def made(env):
    m = bk.create_backup(env)
    return env, m, env.resolved_backup_dir / m["backup_id"]


def problems(env, m):
    r = bk.verify_backup(env, m["backup_id"])
    assert not r.valid
    return "\n".join(r.problems)


def test_verify_detects_missing_file(made):
    env, m, folder = made
    (folder / "files" / "app" / "auth" / "users.json").unlink()
    assert "missing file: app/auth/users.json" in problems(env, m)


def test_verify_detects_modified_file_same_size(made):
    env, m, folder = made
    f = folder / "files" / "historical" / "extraction_summary.csv"
    data = bytearray(f.read_bytes()); data[0] ^= 1
    f.write_bytes(bytes(data))
    assert "content modified (SHA-256 mismatch): historical/extraction_summary.csv" in problems(env, m)


def test_verify_detects_size_change(made):
    env, m, folder = made
    with open(folder / "files" / "historical" / "extraction_summary.csv", "ab") as fh:
        fh.write(b"x")
    assert "size mismatch" in problems(env, m)


def test_verify_detects_unexpected_files(made):
    env, m, folder = made
    write(folder / "files" / "historical" / "Week 30" / "planted.jpg", b"x")
    write(folder / "notes.txt", "x")
    text = problems(env, m)
    assert "unexpected file not in the manifest: historical/Week 30/planted.jpg" in text
    assert "unexpected item in the backup folder: notes.txt" in text


@pytest.mark.parametrize("change,expect", [
    (lambda m: m["files"].pop(), "checksum"),
    (lambda m: m.update(total_bytes=m["total_bytes"] + 1), "checksum"),
    (lambda m: m["files"][0].update(sha256="0" * 64), "checksum"),
])
def test_verify_detects_tampered_manifest(made, change, expect):
    env, m, folder = made
    data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    change(data)
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert expect in problems(env, m)


def test_verify_detects_inconsistent_counts_even_with_rehashed_manifest(made):
    env, m, folder = made
    data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    data["files"].pop()
    data.pop("manifest_sha256")
    data["manifest_sha256"] = hashlib.sha256(bk._canonical(data)).hexdigest()
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert "file count is inconsistent" in problems(env, m)


@pytest.mark.parametrize("bad", ["not json", "[]"])
def test_verify_detects_invalid_manifest(made, bad):
    env, m, folder = made
    (folder / "manifest.json").write_text(bad, encoding="utf-8")
    assert "manifest.json" in problems(env, m)


def test_incomplete_backups_are_never_listed_as_valid(env):
    m = bk.create_backup(env)
    staging = env.resolved_backup_dir / ".staging-BKP-20261119-000000-abcdef"
    shutil.copytree(env.resolved_backup_dir / m["backup_id"], staging)
    no_manifest = env.resolved_backup_dir / "BKP-20261119-000001-abcdef"
    shutil.copytree(env.resolved_backup_dir / m["backup_id"] / "files", no_manifest / "files")
    listing = bk.list_backups(env)
    assert [b["backup_id"] for b in listing["backups"]] == [m["backup_id"]]
    assert {u["folder"] for u in listing["unusable"]} == {staging.name, no_manifest.name}
    assert "missing" in problems(env, {"backup_id": no_manifest.name})


@pytest.mark.parametrize("rel", ["../evil.txt", "/abs/x", "historical/../../x", "C:/x", "historical\\x", "other/x", "historical//x"])
def test_path_traversal_is_rejected(rel):
    with pytest.raises(bk.BackupError):
        bk.safe_relpath(rel)


def test_traversal_entry_in_manifest_fails_verification(made):
    env, m, folder = made
    data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    data["files"][0]["path"] = "historical/../../outside.txt"
    data.pop("manifest_sha256")
    data["manifest_sha256"] = hashlib.sha256(bk._canonical(data)).hexdigest()
    (folder / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert "unsafe path" in problems(env, m)


@pytest.mark.parametrize("bad_id", ["../x", "BKP-1", "BKP-20261119-000000-ABCDEF"])
def test_invalid_backup_ids(env, bad_id):
    assert not bk.verify_backup(env, bad_id).valid


# ============================================================================ restore
def test_restore_into_new_folder_is_complete_and_verified(made, tmp_path):
    env, m, folder = made
    prod_before = tree_hash(tmp_path / "prod"), tree_hash(tmp_path / "appdata")
    dest = tmp_path / "restore" / "copy1"
    dest.parent.mkdir()
    res = bk.restore_backup(env, m["backup_id"], dest)
    assert res["file_count"] == m["file_count"]
    restored = tree_hash(dest)
    for f in m["files"]:
        assert restored[f["path"]] == f["sha256"]
    assert set(restored) == {f["path"] for f in m["files"]} | {"RESTORED_FROM.json", "RESTORE_README.txt"}
    assert "SYNERGY_DATA_ROOT" in (dest / "RESTORE_README.txt").read_text(encoding="utf-8")
    assert (tree_hash(tmp_path / "prod"), tree_hash(tmp_path / "appdata")) == prod_before   # production untouched
    assert not any(p.name.startswith(".restore-staging-") for p in dest.parent.iterdir())
    # the restored copy works as a data root for the dashboard
    from app.services.inspection_store import InspectionStore

    s2 = Settings(data_root=dest / "historical", analysis_dir=dest / "app" / "condition_analyses",
                  import_dir=dest / "app" / "imports", auth_dir=dest / "app" / "auth")
    assert [w.folder.name for w in __import__("app.services.json_loader", fromlist=["x"]).discover_weeks(s2)] == ["Week 30", "week 31"]
    assert InspectionStore.build(s2) is not None
    assert AuditLog(dest / "app" / "auth").verify().valid


def test_restore_into_existing_empty_folder(made, tmp_path):
    env, m, _ = made
    dest = tmp_path / "empty_dest"
    dest.mkdir()
    bk.restore_backup(env, m["backup_id"], dest)
    assert (dest / "historical" / "extraction_summary.csv").is_file()


def test_restore_refuses_non_empty_destination(made, tmp_path):
    env, m, _ = made
    dest = tmp_path / "busy"
    write(dest / "keep.txt", "existing data")
    with pytest.raises(bk.BackupError, match="not empty"):
        bk.restore_backup(env, m["backup_id"], dest)
    assert [p.name for p in dest.iterdir()] == ["keep.txt"] and (dest / "keep.txt").read_text() == "existing data"


@pytest.mark.parametrize("where", ["data_root", "inside_data_root", "analysis", "auth", "backups", "parent_of_prod", "relative"])
def test_restore_refuses_production_and_unsafe_paths(made, tmp_path, where):
    env, m, _ = made
    dest = {"data_root": env.data_root, "inside_data_root": env.data_root / "restored", "analysis": env.analysis_dir,
            "auth": env.resolved_auth_dir / "new", "backups": env.resolved_backup_dir / "r",
            "parent_of_prod": tmp_path, "relative": Path("restored_here")}[where]
    before = tree_hash(tmp_path / "prod")
    with pytest.raises(bk.BackupError):
        bk.restore_backup(env, m["backup_id"], dest)
    assert tree_hash(tmp_path / "prod") == before


def test_restore_refuses_a_corrupted_backup(made, tmp_path):
    env, m, folder = made
    (folder / "files" / "app" / "auth" / "users.json").write_text("tampered")
    dest = tmp_path / "r"
    with pytest.raises(bk.BackupError, match="failed verification"):
        bk.restore_backup(env, m["backup_id"], dest)
    assert not dest.exists()


def test_failed_restore_cleans_up_and_leaves_destination_unchanged(made, tmp_path, monkeypatch):
    env, m, _ = made
    dest = tmp_path / "r2"
    dest.mkdir()
    calls = {"n": 0}
    real = bk._copy_hashed

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("device error")
        return real(src, dst)

    monkeypatch.setattr(bk, "_copy_hashed", flaky)
    with pytest.raises(OSError):
        bk.restore_backup(env, m["backup_id"], dest)
    assert dest.is_dir() and list(dest.iterdir()) == []
    assert not any(p.name.startswith(".restore-staging-") for p in tmp_path.iterdir())


def test_restore_detects_corruption_during_copy(made, tmp_path, monkeypatch):
    env, m, _ = made
    real = bk._copy_hashed
    monkeypatch.setattr(bk, "_copy_hashed", lambda s, d: (real(s, d)[0], "0" * 64))
    dest = tmp_path / "r3"
    with pytest.raises(bk.BackupError, match="does not match the manifest"):
        bk.restore_backup(env, m["backup_id"], dest)
    assert not dest.exists()


# ============================================================================ CLI + audit
def test_cli_commands_and_audit_events(env, tmp_path, monkeypatch, capsys):
    for k, v in {"SYNERGY_DATA_ROOT": env.data_root, "SYNERGY_ANALYSIS_DIR": env.analysis_dir,
                 "SYNERGY_IMPORT_DIR": env.import_dir, "SYNERGY_AUTH_DIR": env.resolved_auth_dir,
                 "SYNERGY_BACKUP_DIR": env.resolved_backup_dir}.items():
        monkeypatch.setenv(k, str(v))
    assert cli_main(["create-backup", "--note", "nightly"]) == 0
    out = capsys.readouterr().out
    assert "created and verified" in out and "does not protect against disk failure" in out
    bid = bk.list_backups(env)["backups"][0]["backup_id"]
    assert cli_main(["list-backups"]) == 0 and bid in capsys.readouterr().out
    assert cli_main(["show-backup", bid]) == 0 and "historical/Week 30" in capsys.readouterr().out
    assert cli_main(["verify-backup", bid]) == 0 and "VALID" in capsys.readouterr().out
    assert cli_main(["restore-backup", bid, "--to", str(env.data_root / "x")]) == 1
    assert "refusing to restore" in capsys.readouterr().out
    assert cli_main(["restore-backup", bid, "--to", str(tmp_path / "restored")]) == 0
    assert "Production data was not changed" in capsys.readouterr().out
    events = [e["event"] for e in reversed(AuditLog(env.resolved_auth_dir).entries())]
    assert events[-4:] == ["backup_created", "backup_verified", "backup_failed", "backup_restored"]
    assert AuditLog(env.resolved_auth_dir).verify().valid


def test_default_backup_folder_is_outside_the_project():
    from app import config

    assert not str(config.DEFAULT_BACKUP_DIR).startswith(str(config.DEFAULT_DATA_ROOT) + os.sep) or "backup-default" in str(config.DEFAULT_BACKUP_DIR)
