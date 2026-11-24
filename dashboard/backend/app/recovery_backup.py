"""Phase 14.4.1 -- RECOVERY-ARTEFACT backups (CLI only; standard library only).

Recovery artefacts are irreplaceable pre-change snapshots kept by the automation pipeline: ``automation/backups/``
(e.g. the pre-AE8 Week 30 output and the image registers from before the 16 Nov Official Location fix) and the
pipeline state in ``automation/.state/``. They are in neither DATA backups (BKP-..., app/backup.py) nor SOURCE backups
(SRC-..., app/source_backup.py), which are both unchanged. Layout::

    <SYNERGY_BACKUP_DIR>/Recovery Artefacts/
        RA-YYYYMMDD-HHMMSS-<6hex>/           (UTC)
            manifest.json    written last: every file (path, size, SHA-256), exclusions, source paths
            files/automation/backups/... , files/automation/.state/...

Only the two allowlisted folders are read. Within them, only recovery file types are copied; secrets (never opened),
temporary/log/cache files, unexpected file types and symbolic links are excluded and recorded. Creation reuses the
data-backup pattern (hash while copying, re-hash, manifest last in ``.staging-RA-*``, one ``os.rename``).
Verification is read-only. There is no restore command: a verified backup is copied back by hand (see the guide).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path, PurePosixPath
from typing import Optional

from . import APP_VERSION
from .backup import BackupError, VerifyReport, _canonical, _copy_hashed, _fsync_write, _now, _within, sha256_file
from .config import Settings

FORMAT_VERSION = "recovery-1"            # distinct from data (1) and source ("source-1") manifests
BACKUP_TYPE = "recovery"
SUBDIR = "Recovery Artefacts"
MANIFEST = "manifest.json"
FILES = "files"
STAGING_PREFIX = ".staging-"
RECOVERY_ID_RE = re.compile(r"^RA-\d{8}-\d{6}-[0-9a-f]{6}$")
PROJECT_ID = "synergy-vessel"
PROJECT_ROOT = Path(__file__).resolve().parents[3]       # module-level so tests can point it at a synthetic tree

SECTIONS = ("automation/backups", "automation/.state")
REQUIRED = ("automation/backups",)                        # .state is optional (recorded as missing if absent)
ALLOWED_EXT_RE = re.compile(r"\.(xlsx|xlsm|xls|csv|json|jpe?g|png)$", re.I)     # recovery snapshot file types
EXCLUDED_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", "node_modules", ".git", "tmp", "temp"}
EXCLUDED_DIR_PREFIXES = (".staging-", ".restore-staging-")
SECRET_RE = re.compile(r"(^\.env($|\.)|\.env$|^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|pfx|p12|keystore|jks|ppk)$|"
                       r"secret|credential|password|passwd|token|api[_-]?key|private[_-]?key)", re.I)
TEMP_RE = re.compile(r"^(.*\.pyc|.*\.pyo|.*\.tmp|~\$.*|\..*\.tmp|thumbs\.db|desktop\.ini|\.ds_store|.*\.log|.*\.bak)$", re.I)


def exclusion_reason(rel: str) -> Optional[str]:
    """Why a project-relative POSIX path must not be in a recovery backup (None = allowed)."""
    parts = PurePosixPath(rel).parts
    if not any(rel.startswith(s + "/") for s in SECTIONS):
        return "outside the recovery allowlist"
    if any(SECRET_RE.search(p) for p in parts):
        return "secret or credential file (never copied, never opened)"
    for p in parts[:-1]:
        if p.lower() in EXCLUDED_DIR_NAMES or p.startswith(EXCLUDED_DIR_PREFIXES):
            return f"excluded folder '{p}' (cache, environment or temporary)"
    if TEMP_RE.match(parts[-1]):
        return "temporary, cache, lock, log or backup-tool file"
    if not ALLOWED_EXT_RE.search(parts[-1]):
        return "not a recovery file type (only Excel, CSV, JSON and image snapshots are kept)"
    return None


def _safe_rel(rel) -> PurePosixPath:
    if (not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel or ":" in rel or "//" in rel
            or rel.endswith("/")):
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part in ("..", ".", "") for part in p.parts) \
            or not any(rel.startswith(s + "/") for s in SECTIONS):
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    return p


# ------------------------------------------------------------------------------------------------ inventory
def collect(project_root: Path) -> tuple[list[tuple[str, Path]], list[dict], list[str]]:
    """(files to copy, exclusions [{path, reason}], missing allowlisted folders). Reads names only."""
    root = Path(project_root).resolve()
    files: list[tuple[str, Path]] = []
    excluded: list[dict] = []
    missing: list[str] = []
    for section in SECTIONS:
        base = root.joinpath(*section.split("/"))
        if base.is_symlink():
            excluded.append({"path": section + "/", "reason": "symbolic link (not followed)"})
            continue
        if not base.is_dir():
            missing.append(section + "/")
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            d = Path(dirpath)
            keep = []
            for name in sorted(dirnames):
                full = d / name
                rel = full.relative_to(root).as_posix()
                if full.is_symlink():
                    excluded.append({"path": rel + "/", "reason": "symbolic link (not followed)"})
                elif name.lower() in EXCLUDED_DIR_NAMES or name.startswith(EXCLUDED_DIR_PREFIXES) or SECRET_RE.search(name):
                    excluded.append({"path": rel + "/", "reason": exclusion_reason(rel + "/x.json") or "excluded folder"})
                else:
                    keep.append(name)
            dirnames[:] = keep
            for name in sorted(filenames):
                full = d / name
                rel = full.relative_to(root).as_posix()
                if full.is_symlink():
                    excluded.append({"path": rel, "reason": "symbolic link (not followed)"})
                    continue
                reason = exclusion_reason(rel)
                if reason:
                    excluded.append({"path": rel, "reason": reason})
                elif _within(full, base):
                    files.append((rel, full))
                else:                                               # defence in depth (junctions etc.)
                    excluded.append({"path": rel, "reason": "resolves outside its allowlisted folder"})
    absent = [r for r in REQUIRED if r + "/" in missing]
    if absent:
        raise BackupError(f"recovery folder missing under {root}: {', '.join(absent)}")
    if not files:
        raise BackupError("no recovery artefacts found to back up")
    return files, excluded, missing


# ------------------------------------------------------------------------------------------------ create
def recovery_backup_dir(settings: Settings) -> Path:
    return settings.resolved_backup_dir / SUBDIR


def create_recovery_backup(settings: Settings, *, note: Optional[str] = None,
                           project_root: Optional[Path] = None) -> dict:
    root = Path(project_root or PROJECT_ROOT).resolve()
    dest_root = recovery_backup_dir(settings).resolve()
    if _within(dest_root, root) or _within(root, dest_root):
        raise BackupError(f"the recovery backup folder {dest_root} overlaps the project {root}; choose another SYNERGY_BACKUP_DIR")
    for prod in (settings.data_root, settings.analysis_dir, settings.import_dir, settings.resolved_auth_dir):
        if _within(dest_root, Path(prod)):
            raise BackupError(f"the recovery backup folder {dest_root} is inside production data {prod}")
    plan, excluded, missing = collect(root)
    for rel, _ in plan:
        _safe_rel(rel)
        if exclusion_reason(rel):
            raise BackupError(f"internal error: excluded path selected for copy: {rel}")
    dest_root.mkdir(parents=True, exist_ok=True)
    need = sum(p.stat().st_size for _, p in plan)
    free = shutil.disk_usage(dest_root).free
    if need * 1.05 + 50 * 1024 * 1024 > free:
        raise BackupError(f"not enough free space: {need / 1e6:.0f} MB needed, {free / 1e6:.0f} MB free")

    now = _now()
    backup_id = f"RA-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    final, staging = dest_root / backup_id, dest_root / f"{STAGING_PREFIX}{backup_id}"
    if final.exists() or staging.exists():
        raise BackupError(f"{backup_id} already exists; nothing was overwritten")
    staging.mkdir()
    try:
        files = []
        for rel, src in plan:
            dst = staging / FILES / Path(*PurePosixPath(rel).parts)
            size, digest = _copy_hashed(src, dst)
            if sha256_file(dst) != digest or dst.stat().st_size != size:
                raise BackupError(f"copy verification failed for {rel}")
            files.append({"path": rel, "size": size, "sha256": digest})
        manifest = {
            "format_version": FORMAT_VERSION, "backup_type": BACKUP_TYPE, "project": PROJECT_ID, "backup_id": backup_id,
            "created_at": now.isoformat(timespec="seconds"), "completed_at": _now().isoformat(timespec="seconds"),
            "app_version": APP_VERSION, "note": (note or "")[:200] or None, "source_root": str(root),
            "source_paths": {s: str(root.joinpath(*s.split("/"))) for s in SECTIONS},
            "missing_allowlisted": missing, "excluded": excluded,
            "file_count": len(files), "total_bytes": sum(f["size"] for f in files), "files": files,
        }
        manifest["manifest_sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
        _fsync_write(staging / MANIFEST, json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8"))
        if read_manifest(staging, expected_id=backup_id)["manifest_sha256"] != manifest["manifest_sha256"]:
            raise BackupError("the written manifest could not be read back")
        os.rename(staging, final)                             # publish: complete or not at all
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)            # only ever our own staging folder
        raise
    return manifest


# ------------------------------------------------------------------------------------------------ read / verify / list
def read_manifest(folder: Path, *, expected_id: Optional[str] = None) -> dict:
    try:
        m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError("manifest.json is missing (incomplete backup)") from None
    except (ValueError, UnicodeDecodeError):
        raise BackupError("manifest.json is not valid JSON") from None
    if not isinstance(m, dict) or m.get("format_version") != FORMAT_VERSION or m.get("backup_type") != BACKUP_TYPE:
        raise BackupError("manifest.json is not a recovery-backup manifest")
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    if m.get("manifest_sha256") != hashlib.sha256(_canonical(body)).hexdigest():
        raise BackupError("manifest.json was modified (its checksum does not match)")
    if m.get("backup_id") != (expected_id or folder.name):
        raise BackupError("manifest backup_id does not match its folder")
    files = m.get("files")
    if not isinstance(files, list) or m.get("file_count") != len(files):
        raise BackupError("manifest file count is inconsistent")
    if m.get("total_bytes") != sum(int(f.get("size", -1)) if isinstance(f, dict) else -1 for f in files):
        raise BackupError("manifest total size is inconsistent")
    return m


def _backup_path(settings: Settings, backup_id: str) -> Path:
    if not RECOVERY_ID_RE.match(backup_id or ""):
        raise BackupError(f"invalid recovery backup id: {backup_id!r}")
    p = recovery_backup_dir(settings).resolve() / backup_id
    if not p.is_dir():
        raise BackupError(f"recovery backup not found: {backup_id}")
    return p


def verify_recovery_backup(settings: Settings, backup_id: str) -> VerifyReport:
    """Read-only: manifest integrity, every file's presence/size/SHA-256, unexpected files, unsafe paths and forbidden
    files. Never repairs, deletes or modifies anything."""
    try:
        folder = _backup_path(settings, backup_id)
        m = read_manifest(folder)
    except BackupError as exc:
        return VerifyReport(backup_id, False, 0, [str(exc)])
    problems, checked, expected = [], 0, set()
    base = folder / FILES
    for f in m["files"]:
        try:
            rel = _safe_rel(f.get("path") if isinstance(f, dict) else None)
        except BackupError as exc:
            problems.append(str(exc))
            continue
        expected.add(rel.as_posix())
        reason = exclusion_reason(rel.as_posix())
        if reason:
            problems.append(f"forbidden file in manifest: {rel} ({reason})")
        target = base.joinpath(*rel.parts)
        if not _within(target, base):
            problems.append(f"path escapes the backup: {rel}")
            continue
        if not target.is_file():
            problems.append(f"missing file: {rel}")
            continue
        checked += 1
        if target.stat().st_size != f.get("size"):
            problems.append(f"size mismatch: {rel} ({target.stat().st_size} bytes, manifest says {f.get('size')})")
        elif sha256_file(target) != f.get("sha256"):
            problems.append(f"content modified (SHA-256 mismatch): {rel}")
    if base.is_dir():
        for p in base.rglob("*"):
            if p.is_file() and p.relative_to(base).as_posix() not in expected:
                rel = p.relative_to(base).as_posix()
                reason = exclusion_reason(rel)
                problems.append(f"unexpected file not in the manifest: {rel}" + (f" (forbidden: {reason})" if reason else ""))
    problems += [f"unexpected item in the backup folder: {p.name}" for p in folder.iterdir() if p.name not in (MANIFEST, FILES)]
    return VerifyReport(backup_id, not problems, checked, problems)


def list_recovery_backups(settings: Settings) -> dict:
    """Recovery backups with an intact manifest (run verify-recovery-backup for a full re-hash), plus unusable folders.
    Reads only the Recovery Artefacts folder."""
    root = recovery_backup_dir(settings)
    out, unusable = [], []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not child.is_dir():
                continue
            if child.name.startswith(STAGING_PREFIX):
                unusable.append({"folder": child.name, "problem": "unfinished recovery backup (interrupted); not usable"})
            elif RECOVERY_ID_RE.match(child.name):
                try:
                    m = read_manifest(child)
                    out.append({**{k: m[k] for k in ("backup_id", "created_at", "file_count", "total_bytes", "note")},
                                "status": "manifest OK"})
                except BackupError as exc:
                    unusable.append({"folder": child.name, "problem": str(exc)})
    return {"backup_dir": str(root), "backups": out, "unusable": unusable}
