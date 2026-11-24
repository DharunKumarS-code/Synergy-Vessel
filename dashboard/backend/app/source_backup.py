"""Phase 14.1 -- SOURCE backups: application source code and recovery documentation (CLI only; standard library only).

Separate from DATA backups (app/backup.py, historical data + dashboard state), which are unchanged. Layout::

    <SYNERGY_BACKUP_DIR>/Source Backups/
        SRC-YYYYMMDD-HHMMSS-<6hex>/          (UTC)
            manifest.json      written last: every file (path, size, SHA-256), exclusions, ENVIRONMENT.txt hash
            ENVIRONMENT.txt    versions, installed packages, rebuild steps, environment-variable NAMES only
            files/<project-relative path>

What is copied is decided by an explicit ALLOWLIST (below). Every candidate file is then checked against hard
exclusions (secrets such as .env, dependency folders, caches, build output, run state, production data, photographs
and Excel workbooks outside test fixtures); every exclusion is recorded in the manifest. ``.env`` contents are never
opened. Creation reuses the data-backup pattern: hash while copying, re-hash the copy, write the manifest last into a
``.staging-SRC-*`` folder, then publish with one ``os.rename``. Verification is read-only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import APP_VERSION
from .backup import (CHUNK, BackupError, VerifyReport, _canonical, _copy_hashed, _fsync_write, _now, _within,  # noqa: F401
                     sha256_file)
from .config import Settings

FORMAT_VERSION = "source-1"                 # distinct from data backups (1): neither tool accepts the other's manifest
BACKUP_TYPE = "source"
SUBDIR = "Source Backups"
MANIFEST = "manifest.json"
ENVIRONMENT = "ENVIRONMENT.txt"
FILES = "files"
STAGING_PREFIX = ".staging-"
SOURCE_ID_RE = re.compile(r"^SRC-\d{8}-\d{6}-[0-9a-f]{6}$")
PROJECT_ID = "synergy-vessel"
# The project folder (E:\Synergy Vessel). Module-level so tests can point it at a synthetic tree.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# ------------------------------------------------------------------------------------------------ allowlist
ALLOW_DIRS = (
    "dashboard/backend/app", "dashboard/backend/tests", "dashboard/backend/experiments",
    "dashboard/frontend/src", "dashboard/frontend/public",
    "automation/src", "automation/tests", "automation/config",
    "automation/output", "automation/live_tests", "automation/logs",       # small operational evidence (filtered)
)
ALLOW_FILES = (
    "dashboard/OPERATOR_GUIDE.md",
    "dashboard/backend/requirements.txt", "dashboard/backend/requirements-dev.txt", "dashboard/backend/pytest.ini",
    "dashboard/frontend/index.html", "dashboard/frontend/package.json", "dashboard/frontend/package-lock.json",
    "dashboard/frontend/vite.config.js", "dashboard/frontend/tailwind.config.js", "dashboard/frontend/postcss.config.js",
    "automation/run_pipeline.py", "automation/extract_images.py", "automation/requirements.txt",
    "automation/requirements-dev.txt", "automation/README.md", "automation/OPERATOR_RUNBOOK.md", "automation/.gitignore",
)
# Allowlist entries whose absence makes the backup useless (the backup is refused).
REQUIRED = ("dashboard/backend/app", "dashboard/frontend/src", "dashboard/frontend/package.json", "automation/src")
SECTIONS = ("dashboard", "automation")

# ------------------------------------------------------------------------------------------------ hard exclusions
EXCLUDED_DIR_NAMES = {"node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                      ".git", "dist", "build", ".cache", "tmp", "temp", "scratchpad", ".state", "backups", "data"}
EXCLUDED_DIR_PREFIXES = (".staging-", ".restore-staging-")
# Always excluded, whatever the extension: .env files (.env, .env.example, prod.env ...) and key/certificate stores.
SECRET_FILE_RE = re.compile(r"(^\.env($|\.)|\.env$|^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|pfx|p12|keystore|jks|ppk)$)", re.I)
# Credential-like words: excluded unless the file is application code/docs (e.g. app/auth/passwords.py is code).
SECRET_WORD_RE = re.compile(r"(secret|credential|password|passwd|token|api[_-]?key|private[_-]?key)", re.I)
CODE_EXT_RE = re.compile(r"\.(py|js|jsx|ts|tsx|css|html|md|svg)$", re.I)


def _secret_name(name: str) -> bool:
    return bool(SECRET_FILE_RE.search(name) or (SECRET_WORD_RE.search(name) and not CODE_EXT_RE.search(name)))
TEMP_FILE_RE = re.compile(r"^(.*\.pyc|.*\.pyo|.*\.tmp|~\$.*|\..*\.tmp|thumbs\.db|desktop\.ini|\.ds_store|.*\.log)$", re.I)
PHOTO_RE = re.compile(r"\.(jpe?g|png|bmp|gif|tiff?|heic|webp)$", re.I)
EXCEL_RE = re.compile(r"\.(xlsx|xlsm|xls|xlsb)$", re.I)
WEEK_RE = re.compile(r"^week\s*\d{1,3}$", re.I)
FIXTURE_PART = "fixtures"                      # synthetic Excel fixtures inside test folders are allowed
VARIABLE_NAMES = (
    "SYNERGY_DATA_ROOT", "SYNERGY_ANALYSIS_DIR", "SYNERGY_IMPORT_DIR", "SYNERGY_AUTH_DIR", "SYNERGY_BACKUP_DIR",
    "SYNERGY_AI_PROVIDER", "SYNERGY_AI_ALLOW_LIVE", "SYNERGY_AI_MOCK_SCENARIO", "SYNERGY_AI_MODEL",
    "SYNERGY_LOCATION_AI_PROVIDER", "SYNERGY_LOCATION_AI_MODEL", "SYNERGY_LOCATION_AI_ALLOW_LIVE",
    "SYNERGY_LOCATION_AI_API_KEY", "GROQ_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY (automation\\.env)", "VITE_API_PROXY_TARGET",
    "VITE_API_BASE_URL",
)


def exclusion_reason(rel: str) -> Optional[str]:
    """Why a project-relative POSIX path must never be in a source backup (None = allowed). Checks every part, so
    it also catches excluded folders anywhere below an allowlisted folder."""
    parts = PurePosixPath(rel).parts
    if not parts:
        return "empty path"
    name = parts[-1]
    if _secret_name(name) or any(SECRET_FILE_RE.search(p) for p in parts[:-1]):
        return "secret or credential file (never copied, never opened)"
    if parts[:2] == ("dashboard", "data"):
        return "dashboard application data (covered by DATA backups)"
    if WEEK_RE.match(parts[0]) or len(parts) == 1:
        return "historical production data or project-root data file (covered by DATA backups)"
    for p in parts[:-1]:
        if p.lower() in EXCLUDED_DIR_NAMES or p.startswith(EXCLUDED_DIR_PREFIXES):
            return f"excluded folder '{p}' (dependencies, caches, build output, run state or data)"
    if TEMP_FILE_RE.match(name):
        return "temporary, cache, lock or log file"
    is_fixture = "tests" in parts and FIXTURE_PART in parts
    if PHOTO_RE.search(name):
        return "photograph (inspection photos belong to DATA backups)"
    if EXCEL_RE.search(name) and not is_fixture:
        return "Excel workbook (production workbooks belong to DATA backups)"
    return None


def _safe_rel(rel: str) -> PurePosixPath:
    if (not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel or ":" in rel or "//" in rel
            or rel.endswith("/")):
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part in ("..", ".", "") for part in p.parts) or p.parts[0] not in SECTIONS:
        raise BackupError(f"unsafe path in manifest: {rel!r}")
    return p


# ------------------------------------------------------------------------------------------------ inventory
def collect(project_root: Path) -> tuple[list[tuple[str, Path]], list[dict], list[str]]:
    """(files to copy [(rel, abs)], exclusions [{path, reason}], missing allowlist entries). Reads names only."""
    root = Path(project_root).resolve()
    files: dict[str, Path] = {}
    excluded: list[dict] = []
    missing: list[str] = []

    def consider(rel: str, path: Path) -> None:
        if path.is_symlink():
            raise BackupError(f"symbolic link not supported (refusing to follow): {path}")
        reason = exclusion_reason(rel)
        if reason:
            excluded.append({"path": rel, "reason": reason})
        else:
            files[rel] = path

    for rel in ALLOW_FILES:
        p = root.joinpath(*rel.split("/"))
        if p.is_file():
            consider(rel, p)
        else:
            missing.append(rel)
    for rel in ALLOW_DIRS:
        base = root.joinpath(*rel.split("/"))
        if not base.is_dir():
            missing.append(rel + "/")
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            d = Path(dirpath)
            keep = []
            for name in sorted(dirnames):
                full = d / name
                sub = full.relative_to(root).as_posix()
                if full.is_symlink():
                    raise BackupError(f"symbolic link not supported (refusing to follow): {full}")
                reason = exclusion_reason(sub + "/x")
                if reason and (name.lower() in EXCLUDED_DIR_NAMES or name.startswith(EXCLUDED_DIR_PREFIXES)
                               or SECRET_FILE_RE.search(name)):
                    excluded.append({"path": sub + "/", "reason": reason})      # whole folder, contents never listed
                else:
                    keep.append(name)
            dirnames[:] = keep
            for name in sorted(filenames):
                full = d / name
                consider(full.relative_to(root).as_posix(), full)
    absent = [r for r in REQUIRED if r in missing or r + "/" in missing]
    if absent:
        raise BackupError(f"essential source missing under {root}: {', '.join(absent)}")
    return sorted(files.items()), excluded, missing


# ------------------------------------------------------------------------------------------------ environment report
def _tool_version(cmd: str) -> str:
    exe = shutil.which(cmd)
    if not exe:
        return "not found on PATH"
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, check=False,
                           env={k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "PATHEXT",
                                                                                     "APPDATA", "USERPROFILE", "TEMP", "TMP")})
        return (r.stdout or r.stderr).strip().splitlines()[0][:80] if (r.stdout or r.stderr).strip() else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def environment_report(backup_id: str, created_at: str) -> str:
    """Non-sensitive rebuild information. Installed packages come from local package metadata (no network, no pip);
    environment variables are listed by NAME only and no value is ever read."""
    pkgs = sorted({f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()
                   if d.metadata and d.metadata.get("Name")}, key=str.lower)
    lines = [
        f"Synergy Vessel SOURCE backup {backup_id} (created {created_at} UTC, app {APP_VERSION})",
        "",
        "This is a SOURCE backup: application code and recovery documentation only. Historical data, photographs,",
        "workbooks, accounts and the audit log are in DATA backups (BKP-...). API keys are never backed up.",
        "",
        "Environment when the backup was made",
        f"  Operating system : {platform.platform()}",
        f"  Python           : {platform.python_version()} ({sys.implementation.name})",
        f"  Node.js          : {_tool_version('node')}",
        f"  npm              : {_tool_version('npm')}",
        "",
        "Installed Python packages (dashboard backend environment):",
        *[f"  {p}" for p in pkgs],
        "",
        "Rebuild (PowerShell), after restoring files\\ to a new project folder:",
        "  1. Install Python 3.11 and Node.js (versions above).",
        "  2. cd dashboard\\backend ; py -3.11 -m venv .venv ; .venv\\Scripts\\python -m pip install -r requirements-dev.txt",
        "     (to reproduce exactly, install the package versions listed above)",
        "  3. cd dashboard\\frontend ; npm ci          (uses package-lock.json)",
        "  4. automation: python -m pip install -r automation\\requirements.txt (uses the system Python 3.11)",
        "  5. Restore a DATA backup (restore-backup) and point SYNERGY_* variables at it; see OPERATOR_GUIDE.md.",
        "  6. Re-enter any API keys by hand. They are not in any backup.",
        "",
        "Environment variable NAMES used by the application (values are never recorded):",
        *[f"  {n}" for n in VARIABLE_NAMES],
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ create
def source_backup_dir(settings: Settings) -> Path:
    return settings.resolved_backup_dir / SUBDIR


def create_source_backup(settings: Settings, *, note: Optional[str] = None, project_root: Optional[Path] = None,
                         environment: Optional[Callable[[str, str], str]] = None) -> dict:
    root = Path(project_root or PROJECT_ROOT).resolve()
    dest_root = source_backup_dir(settings).resolve()
    if _within(dest_root, root) or _within(root, dest_root):
        raise BackupError(f"the source backup folder {dest_root} overlaps the project {root}; choose another SYNERGY_BACKUP_DIR")
    for prod in (settings.data_root, settings.analysis_dir, settings.import_dir, settings.resolved_auth_dir):
        if _within(dest_root, Path(prod)):
            raise BackupError(f"the source backup folder {dest_root} is inside production data {prod}")
    plan, excluded, missing = collect(root)
    for rel, _ in plan:                                       # defence in depth: the rules are re-applied to the plan
        _safe_rel(rel)
        if exclusion_reason(rel):
            raise BackupError(f"internal error: excluded path selected for copy: {rel}")
    dest_root.mkdir(parents=True, exist_ok=True)
    need = sum(p.stat().st_size for _, p in plan)
    free = shutil.disk_usage(dest_root).free
    if need * 1.05 + 50 * 1024 * 1024 > free:
        raise BackupError(f"not enough free space: {need / 1e6:.0f} MB needed, {free / 1e6:.0f} MB free")

    now = _now()
    backup_id = f"SRC-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
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
        created = now.isoformat(timespec="seconds")
        env_bytes = (environment or environment_report)(backup_id, created).encode("utf-8")
        _fsync_write(staging / ENVIRONMENT, env_bytes)
        manifest = {
            "format_version": FORMAT_VERSION, "backup_type": BACKUP_TYPE, "project": PROJECT_ID, "backup_id": backup_id,
            "created_at": created, "completed_at": _now().isoformat(timespec="seconds"), "app_version": APP_VERSION,
            "note": (note or "")[:200] or None, "source_root": str(root),
            "allowlist": {"dirs": list(ALLOW_DIRS), "files": list(ALLOW_FILES)}, "missing_allowlisted": missing,
            "excluded": excluded,
            "environment_file": {"path": ENVIRONMENT, "size": len(env_bytes), "sha256": hashlib.sha256(env_bytes).hexdigest()},
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
        raise BackupError("manifest.json is not a source-backup manifest")
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    if m.get("manifest_sha256") != hashlib.sha256(_canonical(body)).hexdigest():
        raise BackupError("manifest.json was modified (its checksum does not match)")
    if m.get("backup_id") != (expected_id or folder.name):
        raise BackupError("manifest backup_id does not match its folder")
    files = m.get("files")
    if not isinstance(files, list) or m.get("file_count") != len(files):
        raise BackupError("manifest file count is inconsistent")
    if m.get("total_bytes") != sum(int(f.get("size", -1)) for f in files):
        raise BackupError("manifest total size is inconsistent")
    return m


def _backup_path(settings: Settings, backup_id: str) -> Path:
    if not SOURCE_ID_RE.match(backup_id or ""):
        raise BackupError(f"invalid source backup id: {backup_id!r}")
    p = source_backup_dir(settings).resolve() / backup_id
    if not p.is_dir():
        raise BackupError(f"source backup not found: {backup_id}")
    return p


def verify_source_backup(settings: Settings, backup_id: str) -> VerifyReport:
    """Read-only: manifest integrity, ENVIRONMENT.txt, every file's presence/size/SHA-256, unexpected files and
    exclusion violations. Never repairs, deletes or modifies anything."""
    try:
        folder = _backup_path(settings, backup_id)
        m = read_manifest(folder)
    except BackupError as exc:
        return VerifyReport(backup_id, False, 0, [str(exc)])
    problems, checked, expected = [], 0, set()
    base = folder / FILES
    for f in m["files"]:
        try:
            rel = _safe_rel(f.get("path"))
        except BackupError as exc:
            problems.append(str(exc))
            continue
        expected.add(rel.as_posix())
        reason = exclusion_reason(rel.as_posix())
        if reason:
            problems.append(f"exclusion violation in manifest: {rel} ({reason})")
        target = base.joinpath(*rel.parts)
        if not _within(target, base) or not target.is_file():
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
                problems.append(f"unexpected file not in the manifest: {rel}" + (f" (exclusion violation: {reason})" if reason else ""))
    env = m.get("environment_file") or {}
    envp = folder / ENVIRONMENT
    if not envp.is_file():
        problems.append(f"missing file: {ENVIRONMENT}")
    elif envp.stat().st_size != env.get("size") or sha256_file(envp) != env.get("sha256"):
        problems.append(f"content modified (SHA-256 mismatch): {ENVIRONMENT}")
    problems += [f"unexpected item in the backup folder: {p.name}" for p in folder.iterdir()
                 if p.name not in (MANIFEST, FILES, ENVIRONMENT)]
    return VerifyReport(backup_id, not problems, checked, problems)


def list_source_backups(settings: Settings) -> dict:
    """Source backups whose manifest is intact (manifest-level check; run verify-source-backup for a full re-hash),
    plus unusable folders. Reads only the Source Backups folder; data backups are never listed here."""
    root = source_backup_dir(settings)
    out, unusable = [], []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not child.is_dir():
                continue
            if child.name.startswith(STAGING_PREFIX):
                unusable.append({"folder": child.name, "problem": "unfinished source backup (interrupted); not usable"})
            elif SOURCE_ID_RE.match(child.name):
                try:
                    m = read_manifest(child)
                    out.append({**{k: m[k] for k in ("backup_id", "created_at", "file_count", "total_bytes", "note")},
                                "status": "manifest OK"})
                except BackupError as exc:
                    unusable.append({"folder": child.name, "problem": str(exc)})
    return {"backup_dir": str(root), "backups": out, "unusable": unusable}
