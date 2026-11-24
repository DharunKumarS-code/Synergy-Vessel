"""Phase 14.6 -- copy the WHOLE backup collection to a separate physical disk (manual, controlled; never mirroring).

``copy-backups-offline --to F:\\`` creates::

    F:\\Synergy Vessel Offline Backups\\
        YYYY-MM-DD_HHMMSSZ.incomplete\\     while copying / verifying, and kept as-is if anything fails
        YYYY-MM-DD_HHMMSSZ\\                only after every check passed (one os.rename)
            BKP-...\\  Source Backups\\SRC-...\\  Recovery Artefacts\\RA-...\\  Backup Reports\\*.txt
            OFFLINE_COPY_STATUS.txt          IN PROGRESS / FAILED: reason / COMPLETE

Safety:
* the destination must be an absolute path on a plain drive root (no UNC/network path, no folder mount point, no
  symlink/junction); its physical disk is identified (Get-Partition | Get-Disk) and must differ from the disks
  holding the backups and production, and must not be Disk 0. Anything that cannot be identified -> refuse.
* only an allowlist is copied: valid BKP-/SRC-/RA- folders and report .txt files; staging folders and anything
  else are skipped and listed. Every source backup must verify VALID before copying starts.
* files are opened with ``xb`` (never overwrite); nothing is ever deleted, at the source or the destination.
* the copy is verified with the EXISTING verify functions against the copied collection (an isolated Settings
  copy; the environment is not changed), reports are written into the copy, and source and copy inventories are
  compared. The source inventory (path, size, mtime) must be unchanged at the end.
"""

from __future__ import annotations

import ctypes
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import backup as bk
from . import backup_reports as br
from . import recovery_backup as rb
from . import source_backup as sb
from .backup import BackupError, _copy_hashed, _within, sha256_file
from .config import Settings

COLLECTION = "Synergy Vessel Offline Backups"
INCOMPLETE = ".incomplete"
STATUS_FILE = "OFFLINE_COPY_STATUS.txt"
REPORT_NAME_RE = re.compile(r"^(DATA|SOURCE|RECOVERY)_[A-Za-z0-9-]{1,40}_\d{8}T\d{6}Z(_\d{1,2})?\.txt$")
FOLDERS = {  # kind -> (sub folder relative to the backup root, id pattern, verify function)
    "data": ("", bk.BACKUP_ID_RE, bk.verify_backup),
    "source": (sb.SUBDIR, sb.SOURCE_ID_RE, sb.verify_source_backup),
    "recovery": (rb.SUBDIR, rb.RECOVERY_ID_RE, rb.verify_recovery_backup),
}
KNOWN_TOP = {sb.SUBDIR, rb.SUBDIR, br.SUBDIR}

DiskOf = Callable[[Path], dict]


# ------------------------------------------------------------------------------------------------ physical disk
def _volume_root(path: Path) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetVolumePathNameW(str(path), buf, len(buf)):
        raise BackupError(f"cannot determine the volume of {path}")
    return buf.value


def detect_disk(path: Path) -> dict:
    """{'number', 'unique_id', 'bus_type', 'name'} of the ONE physical disk holding ``path``. Fails closed: any doubt
    (non-Windows, network path, folder mount point, spanned volume, missing ids) raises BackupError."""
    if os.name != "nt":
        raise BackupError("physical disk detection is only implemented for Windows; refusing to copy")
    p = Path(path)
    if str(p).startswith(("\\\\", "//")):
        raise BackupError(f"{p} is a network/UNC path; use a locally attached drive")
    root = _volume_root(p)
    if not re.fullmatch(r"[A-Za-z]:\\", root):
        raise BackupError(f"{p} is on a volume mounted at {root!r}, not a drive root; refusing")
    cmd = (f"Get-Partition -DriveLetter {root[0]} -ErrorAction Stop | Get-Disk -ErrorAction Stop | "
           "Select-Object Number,UniqueId,BusType,FriendlyName | ConvertTo-Json -Compress")
    try:
        res = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"physical disk detection failed for {root} ({exc})") from None
    if isinstance(data, list):
        if len(data) != 1:
            raise BackupError(f"{root} spans {len(data)} disks; refusing")
        data = data[0]
    if not isinstance(data, dict) or data.get("Number") is None or not data.get("UniqueId"):
        raise BackupError(f"could not identify the physical disk of {root}; refusing")
    return {"number": int(data["Number"]), "unique_id": str(data["UniqueId"]), "bus_type": str(data.get("BusType")),
            "name": str(data.get("FriendlyName") or "")}


def check_destination(settings: Settings, to: Path, *, disk_of: DiskOf, project_root: Path) -> tuple[Path, dict]:
    """Resolve and vet ``to``; returns (resolved destination, its disk). Raises BackupError on any doubt."""
    raw = Path(to)
    if not raw.is_absolute():
        raise BackupError(f"--to must be an absolute path (got {to})")
    if not raw.is_dir():
        raise BackupError(f"destination {raw} does not exist or is not a folder (is the drive connected?)")
    if raw.is_symlink():
        raise BackupError(f"destination {raw} is a symbolic link/junction; refusing")
    dest = raw.resolve()
    source = settings.resolved_backup_dir.resolve()
    protected = {"the backup folder": source, "the project": Path(project_root).resolve(),
                 "production data": Path(settings.data_root).resolve()}
    for what, p in protected.items():
        if _within(dest, p) or _within(p, dest):
            raise BackupError(f"destination {dest} overlaps {what} ({p}); refusing")
    d = disk_of(dest)
    if d.get("number") == 0:
        raise BackupError(f"destination {dest} is on Disk 0 ({d.get('name')}), the system disk; use an external drive")
    for what, p in protected.items():
        other = disk_of(p)
        if other.get("unique_id") == d.get("unique_id") or other.get("number") == d.get("number"):
            raise BackupError(f"destination {dest} is on the same physical disk as {what} ({d.get('name')}); refusing")
    return dest, d


# ------------------------------------------------------------------------------------------------ plan / inventory
def plan_collection(settings: Settings) -> tuple[list[tuple[str, str]], list[str], list[dict]]:
    """([(kind, backup id)], [report file names], [skipped {path, reason}]) -- names only."""
    root = settings.resolved_backup_dir
    if not root.is_dir():
        raise BackupError(f"backup folder {root} not found")
    backups, reports, skipped = [], [], []
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.is_symlink() and bk.BACKUP_ID_RE.match(child.name):
            backups.append(("data", child.name))
        elif child.is_dir() and not child.is_symlink() and child.name in KNOWN_TOP:
            pass
        else:
            skipped.append({"path": child.name, "reason": "not a backup, report folder or known subfolder"})
    for kind in ("source", "recovery"):
        sub, rx, _ = FOLDERS[kind]
        d = root / sub
        if d.is_dir():
            for child in sorted(d.iterdir()):
                if child.is_dir() and not child.is_symlink() and rx.match(child.name):
                    backups.append((kind, child.name))
                else:
                    skipped.append({"path": f"{sub}/{child.name}", "reason": "staging folder or not a valid backup id"})
    rd = root / br.SUBDIR
    if rd.is_dir():
        for child in sorted(rd.iterdir()):
            if child.is_file() and not child.is_symlink() and REPORT_NAME_RE.match(child.name):
                reports.append(child.name)
            else:
                skipped.append({"path": f"{br.SUBDIR}/{child.name}", "reason": "not a verification report"})
    if not backups:
        raise BackupError(f"no backups found in {root}")
    return backups, reports, skipped


def _rel_folder(kind: str, backup_id: str) -> Path:
    sub = FOLDERS[kind][0]
    return Path(sub) / backup_id if sub else Path(backup_id)


def _files(root: Path, backups, reports) -> dict[str, tuple[int, int]]:
    """{posix rel path: (size, mtime_ns)} for exactly the planned items under ``root``."""
    out = {}
    for kind, bid in backups:
        base = root / _rel_folder(kind, bid)
        for p in sorted(base.rglob("*")):
            if p.is_symlink():
                raise BackupError(f"symbolic link inside backup: {p}")
            if p.is_file():
                st = p.stat()
                out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    for name in reports:
        st = (root / br.SUBDIR / name).stat()
        out[f"{br.SUBDIR}/{name}"] = (st.st_size, st.st_mtime_ns)
    return out


def _write_status(folder: Path, lines: list[str]) -> None:
    tmp = folder / f".{STATUS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, folder / STATUS_FILE)     # only ever our own status file inside our own new folder


# ------------------------------------------------------------------------------------------------ copy
def copy_backups_offline(settings: Settings, to: Path, *, disk_of: Optional[DiskOf] = None,
                         project_root: Optional[Path] = None, now: Optional[dt.datetime] = None,
                         log: Callable[[str], None] = lambda s: None) -> dict:
    """Copy, verify and finalize. Returns a summary dict; raises BackupError (or OSError) on any failure, leaving
    the ``.incomplete`` folder (if one was created) with a FAILED status for inspection. Never deletes anything."""
    disk_of = disk_of or detect_disk
    project_root = Path(project_root or rb.PROJECT_ROOT)
    dest, disk = check_destination(settings, to, disk_of=disk_of, project_root=project_root)
    backups, reports, skipped = plan_collection(settings)
    source = settings.resolved_backup_dir

    log(f"Checking {len(backups)} source backup(s) before copying (read-only) ...")
    for kind, bid in backups:
        r = FOLDERS[kind][2](settings, bid)
        if not r.valid:
            raise BackupError(f"source backup {bid} is INVALID ({r.problems[0]}); nothing was copied")
    before = _files(source, backups, reports)
    need = sum(s for s, _ in before.values())
    free = shutil.disk_usage(dest).free
    if need * 1.1 + 100 * 1024 * 1024 > free:
        raise BackupError(f"not enough free space on {dest}: {need / 1e6:,.0f} MB needed, {free / 1e6:,.0f} MB free")

    now = now or dt.datetime.now(dt.timezone.utc)
    collection = dest / COLLECTION
    name = f"{now:%Y-%m-%d_%H%M%S}Z"
    final, work = collection / name, collection / f"{name}{INCOMPLETE}"
    if final.exists() or work.exists():
        raise BackupError(f"{final} already exists; nothing was overwritten")
    collection.mkdir(exist_ok=True)
    work.mkdir()                                         # fails if it appeared meanwhile
    status = [f"Synergy Vessel offline copy -- started {now.isoformat(timespec='seconds')}",
              f"Source      : {source}", f"Destination : {final}",
              f"Disk        : {disk.get('name')} (disk {disk.get('number')}, {disk.get('bus_type')})"]
    _write_status(work, ["IN PROGRESS"] + status)
    try:
        log(f"Copying {len(before)} files ({need / 1e6:,.1f} MB) to {work} ...")
        for rel in before:
            src, dst = source / rel, work / rel
            size, digest = _copy_hashed(src, dst)
            if size != before[rel][0] or sha256_file(dst) != digest:
                raise BackupError(f"copy check failed for {rel}")

        copied = {p.relative_to(work).as_posix(): p.stat().st_size for p in work.rglob("*")
                  if p.is_file() and p.name != STATUS_FILE}
        if set(copied) != set(before):
            missing, extra = sorted(set(before) - set(copied)), sorted(set(copied) - set(before))
            raise BackupError(f"copied file set differs: {len(missing)} missing, {len(extra)} unexpected "
                              f"({(missing + extra)[:3]})")
        if sum(copied.values()) != need:
            raise BackupError("copied total size differs from the source")

        log("Verifying every copied backup against its manifest ...")
        copy_settings = dataclasses.replace(settings, backup_dir=work)     # isolated; environment untouched
        results, report_paths = [], []
        for kind, bid in backups:
            r = FOLDERS[kind][2](copy_settings, bid)
            report_paths.append(br.write_report(copy_settings, kind, bid, r, exit_status=0 if r.valid else 1,
                                                command=f"copy-backups-offline (verification of the copy in {work})"))
            results.append((kind, bid, r))
        bad = [(bid, r.problems[:2]) for _, bid, r in results if not r.valid]
        if bad:
            raise BackupError(f"{len(bad)} copied backup(s) INVALID: {bad}")
        if _files(source, backups, reports) != before:
            raise BackupError("the SOURCE backup collection changed during the copy; do not trust this copy")

        summary = {"destination": str(final), "disk": disk, "backups": [{"kind": k, "backup_id": b} for k, b in backups],
                   "reports_copied": len(reports), "file_count": len(before), "total_bytes": need, "skipped": skipped,
                   "verification_reports": [str(p).replace(str(work), str(final)) for p in report_paths]}
        _write_status(work, ["COMPLETE (all backups verified VALID)"] + status +
                      [f"Backups     : {len(backups)}", f"Files       : {len(before)} ({need:,} bytes)",
                       f"Verified at : {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}"])
        try:
            os.rename(work, final)                       # never replaces an existing folder on Windows
        except OSError as exc:
            raise BackupError(f"final rename to {final} failed ({exc})") from None
        return summary
    except BaseException as exc:
        try:
            _write_status(work, [f"FAILED: {exc}", "This copy is NOT usable. It was kept for inspection; nothing was "
                                 "deleted. Fix the cause and run the command again."] + status)
        except OSError:
            pass
        raise
