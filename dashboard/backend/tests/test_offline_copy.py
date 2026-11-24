"""Phase 14.6 -- copy-backups-offline. Synthetic projects, backups and "external drives" in temporary folders only.
Physical disks are simulated by a fake disk_of(); the real detector is only exercised read-only and fail-closed."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from app import backup as bk
from app import backup_reports as br
from app import offline_copy as oc
from app import recovery_backup as rb
from app import source_backup as sb
from app.auth.audit import AuditLog
from app.backup import BackupError
from app.cli import main as cli_main
from app.config import Settings

NOW = dt.datetime(2026, 11, 20, 6, 30, 0, tzinfo=dt.timezone.utc)
FINAL = "2026-11-20_063000Z"


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Synergy Vessel"
    write(root / "week 34/Final Official vs AI Location Report.json", "{}")
    write(root / "week 34/Extracted Images - New/IMG_1.jpg", os.urandom(500))
    write(root / "dashboard/backend/app/main.py", "app = 1\n")
    write(root / "dashboard/frontend/src/App.jsx", "x\n")
    write(root / "dashboard/frontend/package.json", "{}")
    write(root / "automation/src/p.py", "x\n")
    write(root / "automation/backups/fix/week 34/Week_34_Image_Register.xlsx", os.urandom(300))
    s = Settings(data_root=root, analysis_dir=tmp_path / "app/analyses", import_dir=tmp_path / "app/imports",
                 auth_dir=tmp_path / "app/auth", backup_dir=tmp_path / "Backups")
    ids = [("data", bk.create_backup(s)["backup_id"]),
           ("source", sb.create_source_backup(s, project_root=root, environment=lambda i, c: "env\n")["backup_id"]),
           ("recovery", rb.create_recovery_backup(s, project_root=root)["backup_id"])]
    br.write_report(s, "data", ids[0][1], bk.verify_backup(s, ids[0][1]), command="c", exit_status=0, now=NOW)
    # things that must be skipped, never copied
    write(s.resolved_backup_dir / ".staging-BKP-20261120-000000-aaaaaa/partial.json", "{}")
    write(s.resolved_backup_dir / "notes.txt", "personal notes")
    write(s.resolved_backup_dir / "BKP-bad-id/x.json", "{}")
    write(s.resolved_backup_dir / ".env", "KEY=SV-OFFLINE-SECRET")
    write(rb.recovery_backup_dir(s) / ".staging-RA-20261120-000000-bbbbbb/x.json", "{}")
    write(br.reports_dir(s) / "random.docx", b"x")
    ext = tmp_path / "EXT"
    ext.mkdir()
    return s, root, ids, ext


def fake_disks(ext: Path, *, ext_disk=None):
    ext_disk = ext_disk or {"number": 1, "unique_id": "USB-123", "bus_type": "USB", "name": "Test USB"}

    def disk_of(p: Path) -> dict:
        return ext_disk if bk._within(Path(p), ext) else {"number": 0, "unique_id": "NVME-0", "bus_type": "NVMe", "name": "Internal"}
    return disk_of


def run(env, **kw):
    s, root, ids, ext = env
    kw.setdefault("disk_of", fake_disks(ext))
    return oc.copy_backups_offline(s, kw.pop("to", ext), project_root=root, now=NOW, **kw)


def inventory(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}


def collection(env) -> Path:
    return env[3] / oc.COLLECTION


# ============================================================================ success
def test_successful_copy_structure_hashes_reports_and_source_unchanged(env):
    s, root, ids, ext = env
    before = inventory(s.resolved_backup_dir)
    summary = run(env)
    final = collection(env) / FINAL
    assert summary["destination"] == str(final) and final.is_dir()
    assert [p.name for p in collection(env).iterdir()] == [FINAL]                     # no .incomplete left
    kinds = {k: b for k, b in ids}
    assert (final / kinds["data"] / "manifest.json").is_file()
    assert (final / "Source Backups" / kinds["source"] / "manifest.json").is_file()
    assert (final / "Recovery Artefacts" / kinds["recovery"] / "manifest.json").is_file()
    # every planned source file is byte-identical in the copy
    for rel, (data, _) in before.items():
        if not rel.startswith((".staging-", "notes.txt", "BKP-bad-id", ".env", "Recovery Artefacts/.staging-",
                               "Backup Reports/random.docx")):
            assert (final / rel).read_bytes() == data, rel
    # skipped items were not copied
    for rel in (".staging-BKP-20261120-000000-aaaaaa", "notes.txt", "BKP-bad-id", ".env",
                "Recovery Artefacts/.staging-RA-20261120-000000-bbbbbb", "Backup Reports/random.docx"):
        assert not (final / rel).exists(), rel
        assert rel in {x["path"] for x in summary["skipped"]}
    # the copied collection verifies with the existing tools
    cs = Settings(data_root=root, auth_dir=s.resolved_auth_dir, backup_dir=final)
    assert bk.verify_backup(cs, kinds["data"]).valid and sb.verify_source_backup(cs, kinds["source"]).valid
    assert rb.verify_recovery_backup(cs, kinds["recovery"]).valid
    # one new VALID report per backup, plus the copied earlier report
    reps = sorted(p.name for p in (final / "Backup Reports").iterdir())
    assert len(reps) == 4 and all(br.KINDS[k] + "_" + b in " ".join(reps) for k, b in ids)
    for p in (final / "Backup Reports").iterdir():
        assert "Result            : VALID" in p.read_text(encoding="utf-8")
    assert (final / oc.STATUS_FILE).read_text(encoding="utf-8").startswith("COMPLETE")
    assert inventory(s.resolved_backup_dir) == before                                 # source untouched
    assert summary["file_count"] == len([r for r in before if r.split("/")[0] in {b for _, b in ids[:1]}
                                         or r.startswith(("Source Backups/SRC", "Recovery Artefacts/RA",
                                                          "Backup Reports/DATA_"))])


def test_second_run_creates_a_new_folder_and_never_touches_the_first(env):
    run(env)
    first = inventory(collection(env) / FINAL)
    s, root, ids, ext = env
    oc.copy_backups_offline(s, ext, project_root=root, now=NOW + dt.timedelta(seconds=5), disk_of=fake_disks(ext))
    assert sorted(p.name for p in collection(env).iterdir()) == [FINAL, "2026-11-20_063005Z"]
    assert inventory(collection(env) / FINAL) == first


# ============================================================================ destination safety
def test_existing_destination_is_refused(env):
    (collection(env) / FINAL).mkdir(parents=True)
    write(collection(env) / FINAL / "keep.txt", "existing")
    with pytest.raises(BackupError, match="already exists"):
        run(env)
    assert [p.name for p in (collection(env) / FINAL).iterdir()] == ["keep.txt"]
    (collection(env) / FINAL / "keep.txt").unlink()
    (collection(env) / FINAL).rmdir()
    (collection(env) / f"{FINAL}.incomplete").mkdir()
    with pytest.raises(BackupError, match="already exists"):
        run(env)


@pytest.mark.parametrize("to", ["relative/path", "Z:/definitely/not/connected/here"])
def test_invalid_or_unavailable_destination(env, to):
    with pytest.raises(BackupError, match="absolute|does not exist"):
        run(env, to=Path(to))


def test_same_physical_disk_is_refused(env):
    s, root, ids, ext = env
    same = {"number": 0, "unique_id": "NVME-0", "bus_type": "NVMe", "name": "Internal"}
    with pytest.raises(BackupError, match="Disk 0"):
        run(env, disk_of=fake_disks(ext, ext_disk=same))
    other_number_same_id = {"number": 3, "unique_id": "NVME-0", "bus_type": "NVMe", "name": "Internal"}
    with pytest.raises(BackupError, match="same physical disk"):
        run(env, disk_of=fake_disks(ext, ext_disk=other_number_same_id))
    assert not collection(env).exists()


def test_destination_inside_or_containing_the_source_is_refused(env):
    s, root, ids, ext = env
    inside = s.resolved_backup_dir / "Recovery Artefacts"
    with pytest.raises(BackupError, match="overlaps the backup folder"):
        run(env, to=inside, disk_of=lambda p: {"number": 1, "unique_id": "X"})
    with pytest.raises(BackupError, match="overlaps"):
        run(env, to=s.resolved_backup_dir.parent, disk_of=lambda p: {"number": 1, "unique_id": "X"})
    with pytest.raises(BackupError, match="overlaps the project"):
        run(env, to=root / "automation", disk_of=lambda p: {"number": 1, "unique_id": "X"})
    assert not (inside / oc.COLLECTION).exists()


def test_disk_detection_failure_fails_closed(env):
    def broken(p):
        raise BackupError("could not identify the physical disk; refusing")
    with pytest.raises(BackupError, match="could not identify"):
        run(env, disk_of=broken)
    assert not collection(env).exists()


def test_real_detector_fails_closed_on_network_paths_and_bad_output(monkeypatch):
    with pytest.raises(BackupError):
        oc.detect_disk(Path(r"\\server\share\backups"))
    if os.name != "nt":
        return
    monkeypatch.setattr(oc, "_volume_root", lambda p: "E:\\")
    for stdout, rc in (("", 0), ("not json", 0), ('[{"Number":1,"UniqueId":"a"},{"Number":2,"UniqueId":"b"}]', 0),
                       ('{"Number":null,"UniqueId":""}', 0), ('{"Number":1,"UniqueId":"a"}', 1)):
        monkeypatch.setattr(oc.subprocess, "run", lambda *a, _o=stdout, _r=rc, **k: type("R", (), {"returncode": _r, "stdout": _o})())
        with pytest.raises(BackupError):
            oc.detect_disk(Path("E:/x"))
    monkeypatch.setattr(oc, "_volume_root", lambda p: "E:\\mnt\\usb\\")
    with pytest.raises(BackupError, match="mounted at"):
        oc.detect_disk(Path("E:/mnt/usb"))


def test_real_detector_parses_one_disk(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows only")
    monkeypatch.setattr(oc, "_volume_root", lambda p: "F:\\")
    out = '{"Number":2,"UniqueId":"USBSTOR-XYZ","BusType":"USB","FriendlyName":"Ext SSD"}'
    monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out})())
    assert oc.detect_disk(Path("F:/")) == {"number": 2, "unique_id": "USBSTOR-XYZ", "bus_type": "USB", "name": "Ext SSD"}


def test_insufficient_space_is_refused_before_copying(env, monkeypatch):
    monkeypatch.setattr(oc.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(BackupError, match="not enough free space"):
        run(env)
    assert not collection(env).exists()


def test_invalid_source_backup_blocks_the_copy(env):
    s, root, ids, ext = env
    rec = rb.recovery_backup_dir(s) / ids[2][1]
    next(p for p in (rec / "files").rglob("*") if p.is_file()).write_bytes(b"corrupt")
    with pytest.raises(BackupError, match="INVALID.*nothing was copied"):
        run(env)
    assert not collection(env).exists()


def test_missing_manifest_in_source_blocks_the_copy(env):
    s, root, ids, ext = env
    (s.resolved_backup_dir / ids[0][1] / "manifest.json").rename(s.resolved_backup_dir / "moved-manifest.json")
    with pytest.raises(BackupError, match="INVALID"):
        run(env)


def test_empty_source_collection_is_refused(tmp_path):
    s = Settings(data_root=tmp_path / "p", auth_dir=tmp_path / "a", backup_dir=tmp_path / "B")
    (tmp_path / "B").mkdir()
    (tmp_path / "EXT").mkdir()
    with pytest.raises(BackupError, match="no backups found"):
        oc.copy_backups_offline(s, tmp_path / "EXT", project_root=tmp_path / "p", disk_of=fake_disks(tmp_path / "EXT"))


# ============================================================================ failures after the copy started
def assert_failed_incomplete(env, words: str):
    inc = collection(env) / f"{FINAL}.incomplete"
    assert inc.is_dir() and not (collection(env) / FINAL).exists()
    status = (inc / oc.STATUS_FILE).read_text(encoding="utf-8")
    assert status.startswith("FAILED:") and words in status and "NOT usable" in status
    return inc


def test_copy_interruption_keeps_a_clearly_failed_incomplete_folder(env, monkeypatch):
    s = env[0]
    before = inventory(s.resolved_backup_dir)
    calls = {"n": 0}
    real = oc._copy_hashed

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("USB disconnected")
        return real(src, dst)

    monkeypatch.setattr(oc, "_copy_hashed", flaky)
    with pytest.raises(OSError, match="USB disconnected"):
        run(env)
    inc = assert_failed_incomplete(env, "USB disconnected")
    assert len([p for p in inc.rglob("*") if p.is_file()]) == 4                       # partial copy kept, not deleted
    assert inventory(s.resolved_backup_dir) == before


def test_corrupted_copy_is_detected(env, monkeypatch):
    real = oc._copy_hashed

    def corrupting(src, dst):
        size, digest = real(src, dst)
        if dst.name == "manifest.json" and "Recovery Artefacts" in str(dst):
            data = dst.read_bytes()
            dst.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        return size, digest

    monkeypatch.setattr(oc, "_copy_hashed", corrupting)
    with pytest.raises(BackupError, match="copy check failed"):
        run(env)
    assert_failed_incomplete(env, "copy check failed")


def test_corruption_missed_by_the_copy_check_is_caught_by_manifest_verification(env, monkeypatch):
    real_copy, real_hash = oc._copy_hashed, oc.sha256_file
    bad = {}

    def corrupting(src, dst):
        size, digest = real_copy(src, dst)
        if "Source Backups" in str(dst) and dst.parent.name != "Source Backups" and dst.name != "manifest.json" and not bad:
            dst.write_bytes(bytes(b ^ 1 for b in dst.read_bytes()))
            bad[str(dst)] = digest
        return size, digest

    monkeypatch.setattr(oc, "_copy_hashed", corrupting)
    monkeypatch.setattr(oc, "sha256_file", lambda p: bad.get(str(p)) or real_hash(p))  # simulate a blind copy check
    with pytest.raises(BackupError, match="1 copied backup\\(s\\) INVALID"):
        run(env)
    inc = assert_failed_incomplete(env, "INVALID")
    reps = list((inc / "Backup Reports").glob("SOURCE_*.txt"))
    assert reps and "Result            : INVALID" in reps[0].read_text(encoding="utf-8")


def test_missing_copied_file_is_detected(env, monkeypatch):
    real = oc._copy_hashed

    def skipping(src, dst):
        size, digest = real(src, dst)
        if dst.name == "ENVIRONMENT.txt":
            dst.unlink()                                       # simulate a file that vanished from the destination
        return size, digest

    monkeypatch.setattr(oc, "_copy_hashed", skipping)
    monkeypatch.setattr(oc, "sha256_file", lambda p: oc.bk.sha256_file(p) if Path(p).exists() else None)
    with pytest.raises(BackupError):
        run(env)
    assert_failed_incomplete(env, "")


def test_unexpected_file_in_destination_is_detected(env, monkeypatch):
    real = oc._copy_hashed

    def injecting(src, dst):
        res = real(src, dst)
        write(dst.parent / "injected.json", "{}")
        return res

    monkeypatch.setattr(oc, "_copy_hashed", injecting)
    with pytest.raises(BackupError, match="unexpected"):
        run(env)
    assert_failed_incomplete(env, "unexpected")


def test_verification_failure_blocks_finalization(env, monkeypatch):
    real = rb.verify_recovery_backup
    monkeypatch.setitem(oc.FOLDERS, "recovery", (rb.SUBDIR, rb.RECOVERY_ID_RE,
                        lambda st, bid: real(st, bid) if st.backup_dir == env[0].backup_dir
                        else bk.VerifyReport(bid, False, 0, ["simulated mismatch"])))
    with pytest.raises(BackupError, match="INVALID"):
        run(env)
    assert_failed_incomplete(env, "simulated mismatch")


def test_report_creation_failure_blocks_finalization(env, monkeypatch):
    monkeypatch.setattr(oc.br, "write_report", lambda *a, **k: (_ for _ in ()).throw(OSError("report disk error")))
    with pytest.raises(OSError):
        run(env)
    assert_failed_incomplete(env, "report disk error")


def test_final_rename_failure_is_reported_and_copy_kept(env, monkeypatch):
    monkeypatch.setattr(oc.os, "rename", lambda a, b: (_ for _ in ()).throw(OSError("access denied")))
    with pytest.raises(BackupError, match="final rename"):
        run(env)
    assert_failed_incomplete(env, "final rename")


def test_source_change_during_copy_blocks_finalization(env, monkeypatch):
    s = env[0]
    real = oc.br.write_report

    def touching(*a, **k):
        p = next(p for p in (s.resolved_backup_dir / "Backup Reports").iterdir() if p.name.startswith("DATA_"))
        os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10_000_000_000))
        return real(*a, **k)

    monkeypatch.setattr(oc.br, "write_report", touching)
    with pytest.raises(BackupError, match="SOURCE backup collection changed"):
        run(env)
    assert_failed_incomplete(env, "changed")


# ============================================================================ CLI + audit
def test_cli_success_and_failure_exit_codes(env, monkeypatch, capsys):
    s, root, ids, ext = env
    for k, v in {"SYNERGY_DATA_ROOT": s.data_root, "SYNERGY_AUTH_DIR": s.resolved_auth_dir,
                 "SYNERGY_BACKUP_DIR": s.resolved_backup_dir}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(oc, "detect_disk", fake_disks(ext))
    monkeypatch.setattr(rb, "PROJECT_ROOT", root)
    assert cli_main(["copy-backups-offline", "--to", str(ext)]) == 0
    out = capsys.readouterr().out
    assert "OFFLINE COPY COMPLETE and VERIFIED" in out and "Safely Remove Hardware" in out
    monkeypatch.setattr(oc, "detect_disk", lambda p: {"number": 0, "unique_id": "NVME-0"})
    assert cli_main(["copy-backups-offline", "--to", str(ext)]) == 1
    out = capsys.readouterr().out
    assert "Error:" in out and "OFFLINE COPY FAILED" in out
    events = [e["event"] for e in reversed(AuditLog(s.resolved_auth_dir).entries())]
    assert events[-2:] == ["offline_copy_completed", "offline_copy_failed"]
    assert AuditLog(s.resolved_auth_dir).verify().valid
    assert os.environ["SYNERGY_BACKUP_DIR"] == str(s.resolved_backup_dir)                # env never redirected
