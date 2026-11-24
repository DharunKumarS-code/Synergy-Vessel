"""Phase 14.4.4 -- save backup VERIFICATION REPORTS (evidence only; never a backup copy).

``verify-backup | verify-source-backup | verify-recovery-backup ID --report`` runs the unchanged verification and then
writes a plain-text report to ``<SYNERGY_BACKUP_DIR>/Backup Reports/<KIND>_<backup id>_<UTC timestamp>.txt``.
The report records exactly what verification returned (VALID / INVALID, exit status, every problem). It contains
paths, sizes and counts only -- never file contents, secrets or environment values. Reports are written to a temporary
file in the same folder, flushed, and renamed into place; an existing report is never overwritten. Nothing inside a
backup folder is opened for writing.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Optional

from .backup import BackupError, VerifyReport
from .config import Settings

SUBDIR = "Backup Reports"
KINDS = {"data": "DATA", "source": "SOURCE", "recovery": "RECOVERY"}
MAX_PROBLEM_LINES = 1000
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def reports_dir(settings: Settings) -> Path:
    return settings.resolved_backup_dir / SUBDIR


def _backup_info(settings: Settings, kind: str, backup_id: str) -> tuple[Optional[Path], str, Optional[int]]:
    """(backup folder, manifest status, total bytes) -- read-only, via each tool's own manifest reader."""
    from . import backup as bk, recovery_backup as rb, source_backup as sb

    path_fn, read = {"data": (bk._backup_path, bk.read_manifest), "source": (sb._backup_path, sb.read_manifest),
                     "recovery": (rb._backup_path, rb.read_manifest)}[kind]
    try:
        folder = path_fn(settings, backup_id)
    except BackupError as exc:
        return None, f"NOT AVAILABLE ({exc})", None
    try:
        m = read(folder)
        return folder, "OK (checksum valid, counts consistent)", m.get("total_bytes")
    except BackupError as exc:
        return folder, f"INVALID ({exc})", None


def _classify(problems: list[str]) -> dict[str, list[str]]:
    groups = {"missing": [], "unexpected": [], "hash": [], "size": [], "forbidden": [], "other": []}
    for p in problems:
        if p.startswith("missing file"):
            groups["missing"].append(p)
        elif p.startswith("unexpected"):
            groups["unexpected"].append(p)
        elif "SHA-256 mismatch" in p:
            groups["hash"].append(p)
        elif p.startswith("size mismatch"):
            groups["size"].append(p)
        elif p.startswith(("exclusion violation", "forbidden file")):
            groups["forbidden"].append(p)
        else:
            groups["other"].append(p)
    return groups


def render(kind: str, backup_id: str, report: VerifyReport, *, location: str, manifest_status: str,
           size_bytes: Optional[int], command: str, exit_status: int, verified_at: dt.datetime) -> str:
    g = _classify(report.problems)
    result = "VALID" if report.valid else "INVALID"
    lines = [
        f"Synergy Vessel backup verification report -- {result}",
        "",
        f"Verified at (UTC) : {verified_at.isoformat(timespec='seconds')}",
        f"Backup type       : {KINDS[kind]}",
        f"Backup ID         : {backup_id}",
        f"Backup location   : {location}",
        f"Result            : {result}",
        f"Exit status       : {exit_status}",
        f"Files checked     : {report.files_checked}",
        f"Backup size       : {f'{size_bytes:,} bytes ({size_bytes / 1e6:,.1f} MB)' if size_bytes is not None else 'unknown'}",
        f"Manifest status   : {manifest_status}",
        f"Missing files     : {len(g['missing'])}",
        f"Unexpected files  : {len(g['unexpected'])}",
        f"Hash mismatches   : {len(g['hash'])}",
        f"Size mismatches   : {len(g['size'])}",
        f"Forbidden files   : {len(g['forbidden'])}",
        f"Other errors      : {len(g['other'])}",
        f"Command           : {command}",
        "",
        "Problems:" if report.problems else "Problems: none",
    ]
    shown = report.problems[:MAX_PROBLEM_LINES]
    lines += [f"  - {p}" for p in shown]
    if len(report.problems) > len(shown):
        lines.append(f"  ... and {len(report.problems) - len(shown)} more")
    lines += ["", "This report is evidence of a verification run. It is NOT a backup copy."]
    return "\n".join(lines) + "\n"


def write_report(settings: Settings, kind: str, backup_id: str, report: VerifyReport, *, command: str,
                 exit_status: int, now: Optional[dt.datetime] = None) -> Path:
    """Write one report atomically; never overwrite an existing report. Returns the report path."""
    if kind not in KINDS:
        raise ValueError(f"unknown backup kind: {kind}")
    if exit_status != (0 if report.valid else 1):
        raise ValueError("exit status does not match the verification result")    # never record a false success
    now = now or dt.datetime.now(dt.timezone.utc)
    folder, manifest_status, size = _backup_info(settings, kind, backup_id)
    text = render(kind, backup_id, report, location=str(folder) if folder else "not found",
                  manifest_status=manifest_status, size_bytes=size, command=command, exit_status=exit_status,
                  verified_at=now)
    out_dir = reports_dir(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = backup_id if _SAFE_ID_RE.match(backup_id or "") else "INVALID-ID"
    stem = f"{KINDS[kind]}_{safe_id}_{now:%Y%m%dT%H%M%SZ}"
    for n in range(1, 100):
        final = out_dir / (f"{stem}.txt" if n == 1 else f"{stem}_{n}.txt")
        if final.exists():
            continue
        tmp = out_dir / f".{final.name}.tmp"
        try:
            with open(tmp, "x", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, final)                  # atomic and never overwrites: fails if the name exists
            except FileExistsError:
                continue
            return final
        finally:
            if tmp.exists():
                tmp.unlink()
    raise BackupError(f"could not choose a free report name for {stem}")
