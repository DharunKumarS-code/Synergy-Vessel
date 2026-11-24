"""Phase 8.3 -- backup creation, verification and safe restore (CLI only; standard library only).

Backup layout (under SYNERGY_BACKUP_DIR)::

    BKP-YYYYMMDD-HHMMSS-<6hex>/
        manifest.json          written last; lists every file with size + SHA-256
        files/historical/...   week folders + root data files  (restores as SYNERGY_DATA_ROOT)
        files/app/condition_analyses/...                        (SYNERGY_ANALYSIS_DIR)
        files/app/imports/...                                   (SYNERGY_IMPORT_DIR)
        files/app/auth/...                                      (SYNERGY_AUTH_DIR)

Safety:
* Sources are only ever opened for reading. Each file is hashed while it is copied, the copy is
  hashed again, and any mismatch aborts the whole backup.
* Work happens in ``.staging-BKP-*``; the manifest is written after every file is verified, then
  one ``os.rename`` publishes the backup. Folders without a valid manifest are never listed as backups.
* Restore verifies first, writes only into a new/empty destination outside every production
  folder, re-verifies the restored copy in staging, then publishes it with one rename.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from . import APP_VERSION
from .config import Settings

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
FILES = "files"
STAGING_PREFIX = ".staging-"
RESTORE_STAGING_PREFIX = ".restore-staging-"
BACKUP_ID_RE = re.compile(r"^BKP-\d{8}-\d{6}-[0-9a-f]{6}$")
WEEK_NAME_RE = re.compile(r"^week\s*\d{1,3}$", re.I)
SECTIONS = ("historical", "app/condition_analyses", "app/imports", "app/auth")
CHUNK = 1024 * 1024

# Never backed up. Every exclusion is counted in the manifest (nothing is skipped silently).
EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", ".git", ".mypy_cache"}
EXCLUDED_DIR_PREFIXES = (".staging-", ".restore-staging-")
EXCLUDED_FILE_RE = re.compile(r"^(\.env.*|.*\.env|.*\.tmp|~\$.*|\..*\.tmp|thumbs\.db|desktop\.ini|\.ds_store|.*\.pyc|.*\.log)$", re.I)


class BackupError(RuntimeError):
    pass


@dataclass
class VerifyReport:
    backup_id: Optional[str]
    valid: bool
    files_checked: int = 0
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = f"{self.backup_id or '?'}: {'VALID' if self.valid else 'INVALID'} ({self.files_checked} files checked)"
        return "\n".join([head] + [f"  - {p}" for p in self.problems[:50]]
                         + ([f"  ... and {len(self.problems) - 50} more"] if len(self.problems) > 50 else []))


# ============================================================================ helpers
def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def safe_relpath(rel: str) -> PurePosixPath:
    """Validate a manifest path: relative, POSIX, no '..', no drive, under a known section."""
    if (not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel or ":" in rel
            or "//" in rel or rel.endswith("/")):
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part in ("..", ".", "") for part in p.parts):
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    if not any(rel == s or rel.startswith(s + "/") for s in SECTIONS):
        raise BackupError(f"path outside the known backup sections: {rel!r}")
    return p


def _fsync_write(path: Path, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _copy_hashed(src: Path, dst: Path) -> tuple[int, str]:
    """Copy src -> dst (read-only on src), hashing the bytes as they are read. Returns (size, sha256)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    h, size = hashlib.sha256(), 0
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        for chunk in iter(lambda: fin.read(CHUNK), b""):
            h.update(chunk)
            fout.write(chunk)
            size += len(chunk)
        fout.flush()
        os.fsync(fout.fileno())
    return size, h.hexdigest()


# ============================================================================ sources
@dataclass
class Source:
    section: str      # e.g. "historical/Week 30" or "app/auth"
    path: Path        # absolute source path (file or folder)
    kind: str         # "historical_week" | "historical_file" | "app"


def collect_sources(settings: Settings) -> list[Source]:
    root = settings.data_root.resolve()
    out: list[Source] = []
    if not root.is_dir():
        raise BackupError(f"data root not found: {root}")
    app_dirs = {"app/condition_analyses": settings.analysis_dir, "app/imports": settings.import_dir,
                "app/auth": settings.resolved_auth_dir}
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            if (child / settings.report_filename).is_file() or WEEK_NAME_RE.match(child.name):
                out.append(Source(f"historical/{child.name}", child, "historical_week"))
        elif child.is_file() and not EXCLUDED_FILE_RE.match(child.name):
            out.append(Source(f"historical/{child.name}", child, "historical_file"))
    if not any(s.kind == "historical_week" for s in out):
        raise BackupError(f"no historical week folders found under {root}")
    for section, path in app_dirs.items():
        out.append(Source(section, Path(path).resolve(), "app"))
    return out


def _walk(source: Source, excluded: list[dict]) -> Iterable[tuple[str, Path]]:
    """Yield (relative manifest path, absolute file) for one source. Missing app folders yield nothing."""
    if source.kind == "historical_file":
        yield source.section, source.path
        return
    base = source.path
    if not base.exists():
        return  # e.g. no imports yet -- recorded in the manifest as an empty section
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        d = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            full = d / name
            if name in EXCLUDED_DIRS or name.startswith(EXCLUDED_DIR_PREFIXES):
                excluded.append({"path": f"{source.section}/{full.relative_to(base).as_posix()}/", "reason": "temporary/cache folder"})
            elif full.is_symlink():
                raise BackupError(f"symbolic link not supported (refusing to follow): {full}")
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            full = d / name
            rel = f"{source.section}/{full.relative_to(base).as_posix()}"
            if EXCLUDED_FILE_RE.match(name):
                excluded.append({"path": rel, "reason": "secret, temporary or log file"})
                continue
            if full.is_symlink():
                raise BackupError(f"symbolic link not supported (refusing to follow): {full}")
            yield rel, full


# ============================================================================ create
def create_backup(settings: Settings, *, note: Optional[str] = None) -> dict:
    backup_root = settings.resolved_backup_dir.resolve()
    sources = collect_sources(settings)
    for s in sources:
        if _within(backup_root, s.path) or _within(s.path, backup_root):
            raise BackupError(f"the backup folder {backup_root} overlaps the source {s.path}; choose another SYNERGY_BACKUP_DIR")
    backup_root.mkdir(parents=True, exist_ok=True)

    now = _now()
    backup_id = f"BKP-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    final = backup_root / backup_id
    staging = backup_root / f"{STAGING_PREFIX}{backup_id}"
    if final.exists() or staging.exists():
        raise BackupError(f"{backup_id} already exists; nothing was overwritten")

    excluded: list[dict] = []
    plan = [(rel, path) for s in sources for rel, path in _walk(s, excluded)]
    need = sum(p.stat().st_size for _, p in plan)
    free = shutil.disk_usage(backup_root).free
    if need * 1.05 + 50 * 1024 * 1024 > free:
        raise BackupError(f"not enough free space: {need / 1e6:.0f} MB needed, {free / 1e6:.0f} MB free")

    staging.mkdir()
    try:
        files = []
        for rel, src in plan:
            safe_relpath(rel)
            dst = staging / FILES / Path(*PurePosixPath(rel).parts)
            size, digest = _copy_hashed(src, dst)
            if sha256_file(dst) != digest or dst.stat().st_size != size:
                raise BackupError(f"copy verification failed for {rel}")
            files.append({"path": rel, "size": size, "sha256": digest})
        manifest = {
            "format_version": FORMAT_VERSION,
            "backup_id": backup_id,
            "created_at": now.isoformat(timespec="seconds"),
            "completed_at": _now().isoformat(timespec="seconds"),
            "app_version": APP_VERSION,
            "note": (note or "")[:200] or None,
            "sources": [{"section": s.section, "path": str(s.path), "kind": s.kind, "present": s.path.exists()} for s in sources],
            "restore_targets": {"historical": "SYNERGY_DATA_ROOT", "app/condition_analyses": "SYNERGY_ANALYSIS_DIR",
                                "app/imports": "SYNERGY_IMPORT_DIR", "app/auth": "SYNERGY_AUTH_DIR"},
            "excluded": excluded,
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "files": files,
        }
        manifest["manifest_sha256"] = hashlib.sha256(_canonical(manifest)).hexdigest()
        _fsync_write(staging / MANIFEST, json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8"))
        os.rename(staging, final)  # publish: a backup either exists complete or not at all
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


# ============================================================================ list / read / verify
def _backup_path(settings: Settings, backup_id: str) -> Path:
    if not BACKUP_ID_RE.match(backup_id or ""):
        raise BackupError(f"invalid backup id: {backup_id!r}")
    p = settings.resolved_backup_dir.resolve() / backup_id
    if not p.is_dir():
        raise BackupError(f"backup not found: {backup_id}")
    return p


def read_manifest(folder: Path) -> dict:
    try:
        m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError("manifest.json is missing (incomplete backup)") from None
    except (ValueError, UnicodeDecodeError):
        raise BackupError("manifest.json is not valid JSON") from None
    if not isinstance(m, dict) or m.get("format_version") != FORMAT_VERSION:
        raise BackupError("manifest.json has an unsupported format")
    claimed = m.get("manifest_sha256")
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    if claimed != hashlib.sha256(_canonical(body)).hexdigest():
        raise BackupError("manifest.json was modified (its checksum does not match)")
    if m.get("backup_id") != folder.name:
        raise BackupError("manifest backup_id does not match its folder")
    files = m.get("files")
    if not isinstance(files, list) or m.get("file_count") != len(files):
        raise BackupError("manifest file count is inconsistent")
    if m.get("total_bytes") != sum(int(f.get("size", -1)) for f in files):
        raise BackupError("manifest total size is inconsistent")
    return m


def list_backups(settings: Settings) -> dict:
    root = settings.resolved_backup_dir
    out, incomplete = [], []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not child.is_dir():
                continue
            if child.name.startswith(STAGING_PREFIX):
                incomplete.append({"folder": child.name, "problem": "unfinished backup (interrupted); not usable"})
            elif BACKUP_ID_RE.match(child.name):
                try:
                    m = read_manifest(child)
                    out.append({k: m[k] for k in ("backup_id", "created_at", "app_version", "file_count", "total_bytes", "note")})
                except BackupError as exc:
                    incomplete.append({"folder": child.name, "problem": str(exc)})
    return {"backup_dir": str(root), "backups": out, "unusable": incomplete}


def show_backup(settings: Settings, backup_id: str) -> dict:
    m = read_manifest(_backup_path(settings, backup_id))
    sections: dict[str, dict] = {}
    for f in m["files"]:
        parts = f["path"].split("/")
        key = "/".join(parts[:2]) if parts[0] == "historical" and len(parts) > 2 else (
            "historical (root files)" if parts[0] == "historical" else "/".join(parts[:2]))
        s = sections.setdefault(key, {"files": 0, "bytes": 0})
        s["files"] += 1
        s["bytes"] += f["size"]
    return {**{k: v for k, v in m.items() if k not in ("files", "excluded")},
            "excluded_count": len(m["excluded"]), "excluded": m["excluded"][:20], "sections": sections}


def _verify_tree(base: Path, files: list[dict], label: str) -> tuple[int, list[str]]:
    problems, checked, expected = [], 0, set()
    for f in files:
        try:
            rel = safe_relpath(f.get("path"))
        except BackupError as exc:
            problems.append(str(exc))
            continue
        expected.add(rel.as_posix())
        target = base.joinpath(*rel.parts)
        if not _within(target, base):
            problems.append(f"path escapes the {label}: {f.get('path')}")
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
                problems.append(f"unexpected file not in the manifest: {p.relative_to(base).as_posix()}")
    return checked, problems


def verify_backup(settings: Settings, backup_id: str) -> VerifyReport:
    try:
        folder = _backup_path(settings, backup_id)
        m = read_manifest(folder)
    except BackupError as exc:
        return VerifyReport(backup_id, False, 0, [str(exc)])
    checked, problems = _verify_tree(folder / FILES, m["files"], "backup")
    extra = [p.name for p in folder.iterdir() if p.name not in (MANIFEST, FILES)]
    problems += [f"unexpected item in the backup folder: {n}" for n in extra]
    return VerifyReport(backup_id, not problems, checked, problems)


# ============================================================================ restore
def _protected_paths(settings: Settings) -> list[Path]:
    return [settings.data_root, settings.analysis_dir, settings.import_dir, settings.resolved_auth_dir,
            settings.resolved_backup_dir, Path(__file__).resolve().parents[1]]


def check_destination(settings: Settings, dest: Path) -> Path:
    dest = Path(dest).expanduser()
    if not dest.is_absolute():
        raise BackupError("the restore destination must be an absolute path")
    if dest.is_symlink():
        raise BackupError("the restore destination must not be a symbolic link")
    resolved = dest.resolve()
    for p in _protected_paths(settings):
        p = Path(p).resolve()
        if _within(resolved, p) or _within(p, resolved):
            raise BackupError(f"refusing to restore into or around a production/application folder: {p}")
    if resolved.exists():
        if not resolved.is_dir():
            raise BackupError("the restore destination exists and is not a folder")
        if any(resolved.iterdir()):
            raise BackupError("the restore destination is not empty; restore only into a new or empty folder")
    if not resolved.parent.is_dir():
        raise BackupError(f"the parent folder of the destination does not exist: {resolved.parent}")
    return resolved


RESTORE_README = """Synergy Vessel restored copy of backup {backup_id} (created {created_at}).

Nothing in production was changed. To run the dashboard against this copy, stop the backend and start
it with these environment variables (PowerShell example):

  $env:SYNERGY_DATA_ROOT    = "{dest}\\historical"
  $env:SYNERGY_ANALYSIS_DIR = "{dest}\\app\\condition_analyses"
  $env:SYNERGY_IMPORT_DIR   = "{dest}\\app\\imports"
  $env:SYNERGY_AUTH_DIR     = "{dest}\\app\\auth"

Check the restored dashboard first. Replacing the production folders with this copy is a deliberate,
manual step: stop the backend, move the current production folders aside (do not delete them), copy
these folders into place, start the backend and verify. Run 'verify-audit' afterwards.
"""


def restore_backup(settings: Settings, backup_id: str, dest: Path) -> dict:
    report = verify_backup(settings, backup_id)
    if not report.valid:
        raise BackupError("the backup failed verification; nothing was restored:\n" + report.summary())
    dest = check_destination(settings, dest)
    folder = _backup_path(settings, backup_id)
    m = read_manifest(folder)
    staging = dest.parent / f"{RESTORE_STAGING_PREFIX}{dest.name}-{secrets.token_hex(3)}"
    staging.mkdir()
    try:
        for f in m["files"]:
            rel = safe_relpath(f["path"])
            target = staging.joinpath(*rel.parts)
            if not _within(target, staging):
                raise BackupError(f"path escapes the destination: {rel}")
            size, digest = _copy_hashed(folder / FILES / Path(*rel.parts), target)
            if size != f["size"] or digest != f["sha256"]:
                raise BackupError(f"restored file does not match the manifest: {rel}")
        for section in ("historical", "app/condition_analyses", "app/imports", "app/auth"):
            staging.joinpath(*section.split("/")).mkdir(parents=True, exist_ok=True)
        checked, problems = _verify_tree(staging, m["files"], "restore")
        problems = [p for p in problems if not p.startswith("unexpected file")]  # only README/marker are added below
        if problems or checked != m["file_count"]:
            raise BackupError("the restored copy failed verification:\n  - " + "\n  - ".join(problems[:20]))
        _fsync_write(staging / "RESTORED_FROM.json", json.dumps({
            "backup_id": backup_id, "backup_created_at": m["created_at"], "restored_at": _now().isoformat(timespec="seconds"),
            "file_count": m["file_count"], "total_bytes": m["total_bytes"], "manifest_sha256": m["manifest_sha256"]}, indent=1).encode())
        _fsync_write(staging / "RESTORE_README.txt",
                     RESTORE_README.format(backup_id=backup_id, created_at=m["created_at"], dest=dest).encode("utf-8"))
        if dest.exists():
            dest.rmdir()  # only ever an empty folder (checked above); rmdir refuses anything else
        os.rename(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"backup_id": backup_id, "destination": str(dest), "file_count": m["file_count"], "total_bytes": m["total_bytes"]}
