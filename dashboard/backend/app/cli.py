"""Administration commands. Run from dashboard/backend:

    .venv\\Scripts\\python -m app.cli create-admin     create an administrator interactively
    .venv\\Scripts\\python -m app.cli verify-audit     check the audit log's hash chain
    .venv\\Scripts\\python -m app.cli create-backup [--note TEXT]
    .venv\\Scripts\\python -m app.cli list-backups | show-backup ID | verify-backup ID
    .venv\\Scripts\\python -m app.cli restore-backup ID --to EMPTY_FOLDER   (never into production)
    .venv\\Scripts\\python -m app.cli create-source-backup [--note TEXT] | list-source-backups | verify-source-backup ID
    .venv\\Scripts\\python -m app.cli create-recovery-backup [--note TEXT] | list-recovery-backups | verify-recovery-backup ID
    (every verify-* command accepts --report: also save the result in <backup folder>\\Backup Reports)
    .venv\\Scripts\\python -m app.cli copy-backups-offline --to F:\\      (external drive only; copy + verify)
    .venv\\Scripts\\python -m app.cli generate-viewpoints [--dry-run | --out FOLDER] [--replace-proposals]
        (SV-08: propose the viewpoint mapping; read-only on historical data, nothing is approved automatically)

Passwords are read with getpass (no echo), confirmed, and never printed or logged. No account is ever
created automatically and no default credentials exist.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Callable, Optional

from .auth.audit import AuditLog
from .auth.passwords import password_problem
from .auth.users import AccountError, UserStore, normalise_username
from .config import Settings, get_settings

CLI_ACTOR = {"user_id": "cli", "username": "local-cli", "role": "system"}


def create_admin(auth_dir, *, ask: Optional[Callable[[str], str]] = None,
                 ask_secret: Optional[Callable[[str], str]] = None, out=print) -> int:
    ask, ask_secret = ask or input, ask_secret or getpass.getpass  # resolved at call time
    users, audit = UserStore(auth_dir), AuditLog(auth_dir)
    out(f"Creating an administrator account in {users.path} (press Ctrl+C to cancel)")
    try:
        username = normalise_username(ask("Username: "))
    except AccountError as exc:
        out(f"Error: {exc.message}")
        return 2
    if users.by_username(username):
        out(f"Error: an account named '{username}' already exists. Nothing was changed.")
        return 3
    display_name = ask("Display name (optional): ").strip() or None
    for _ in range(3):
        pw = ask_secret("Password (input hidden): ")
        problem = password_problem(pw, username)
        if problem:
            out(f"Error: {problem}")
            continue
        if ask_secret("Repeat password: ") != pw:
            out("Error: the passwords do not match.")
            continue
        break
    else:
        out("No account was created.")
        return 4
    try:
        user = users.create(username, pw, "admin", display_name)
    except AccountError as exc:
        out(f"Error: {exc.message} Nothing was changed.")
        return 3
    audit.record("user_created", actor=CLI_ACTOR, target=user.user_id,
                 details={"username": user.username, "role": "admin", "via": "cli"})
    out(f"Administrator '{user.username}' created ({user.user_id}). Sign in from the dashboard.")
    return 0


def verify_audit(auth_dir, out=print) -> int:
    result = AuditLog(auth_dir).verify()
    out(f"Audit log: {result.entries} entries - {'VALID' if result.valid else 'PROBLEMS FOUND'}")
    for p in result.problems:
        out(f"  - {p}")
    return 0 if result.valid else 1


def generate_viewpoints_command(settings: Settings, args, out=print) -> int:
    """SV-08: propose the viewpoint mapping from the loaded inspection data. Historical files are only read. The
    mapping is written to the viewpoint folder (or --out); a mapping that already holds administrator decisions is
    never overwritten."""
    from .imports.store import write_atomic
    from .services import viewpoints as vp
    from .services.inspection_store import InspectionStore

    store = InspectionStore.build(settings)
    data = vp.generate(store.records, settings.unlabelled_placeholder)
    val = vp.validate_mapping(data, store.by_id)
    if not val.ok:
        out("Error: the generated mapping failed validation: " + "; ".join(val.errors[:5]))
        return 1
    s = vp.summarise(data)
    out(f"Records {len(store.records)} | viewpoints {s['viewpoints']} | linked {s['linked_records']} | "
        f"members {s['members']} | findings {s['findings']}")
    if args.dry_run:
        out("Dry run: nothing was written.")
        return 0
    folder = Path(args.out or settings.resolved_viewpoint_dir).resolve()
    files = vp.ViewpointStore(folder)
    with files.lock:
        existing = None
        if files.path.exists():
            try:
                existing = files.read()
            except (OSError, ValueError):
                out(f"Error: {files.path} exists but cannot be read. Nothing was written; restore it from a backup.")
                return 3
        if existing is not None:
            if vp.has_decisions(existing):
                out(f"Error: {files.path} already contains administrator decisions. It was NOT overwritten. "
                    "Review changes in the dashboard instead (Admin > Viewpoints).")
                return 3
            if not args.replace_proposals:
                out(f"Error: {files.path} already exists (proposals only). Use --replace-proposals to regenerate it.")
                return 3
            data["revision"] = int(existing.get("revision") or 0) + 1
        files.write(data)
        write_atomic(folder, vp.REPORT_FILE, vp.render_report(data, len(store.records)).encode("utf-8"))
    if args.out is None:
        AuditLog(settings.resolved_auth_dir).record(
            "viewpoints_generated", actor=CLI_ACTOR, target=vp.MAPPING_FILE,
            details={"revision": data["revision"], "viewpoints": s["viewpoints"], "linked_records": s["linked_records"],
                     "members": s["members"], "replaced_proposals": existing is not None, "via": "cli"})
    out(f"Wrote {files.path} and {folder / vp.REPORT_FILE}. Nothing is approved yet: review in Admin > Viewpoints.")
    return 0


def _mb(n: int) -> str:
    return f"{n / 1e6:,.1f} MB"


def _verify_parser(sub, name: str, text: str) -> None:
    p = sub.add_parser(name, help=text)
    p.add_argument("backup_id")
    p.add_argument("--report", action="store_true",
                   help="also save a verification report in <backup folder>\\Backup Reports (evidence, not a backup)")


def _finish_verify(settings: Settings, kind: str, args, r, out) -> int:
    """Print the verification result and, with --report, save it. The exit status is the verification result
    (0 VALID, 1 INVALID); if the report cannot be saved the command says so and returns 3 (or 1 if INVALID)."""
    status = 0 if r.valid else 1
    out(r.summary())
    if getattr(args, "report", False):
        from .backup import BackupError
        from .backup_reports import write_report

        try:
            path = write_report(settings, kind, args.backup_id, r, exit_status=status,
                                command=f"python -m app.cli {args.command} {args.backup_id} --report")
            out(f"Verification report saved: {path}")
        except (OSError, BackupError, ValueError) as exc:
            out(f"Error: the verification report was NOT saved ({exc}). The result above still stands.")
            return status or 3
    return status


def backup_command(settings: Settings, args, out=print) -> int:
    from . import backup as bk

    audit = AuditLog(settings.resolved_auth_dir)
    try:
        if args.command == "create-backup":
            out(f"Creating a backup in {settings.resolved_backup_dir} (sources are only read) ...")
            m = bk.create_backup(settings, note=args.note)
            audit.record("backup_created", actor=CLI_ACTOR, target=m["backup_id"],
                         details={"file_count": m["file_count"], "total_bytes": m["total_bytes"], "excluded": len(m["excluded"])})
            out(f"Backup {m['backup_id']} created and verified: {m['file_count']} files, {_mb(m['total_bytes'])}, "
                f"{len(m['excluded'])} excluded item(s).")
            out("Note: a backup on the same disk does not protect against disk failure; copy it to another drive.")
            return 0
        if args.command == "list-backups":
            res = bk.list_backups(settings)
            out(f"Backups in {res['backup_dir']}:")
            if not res["backups"]:
                out("  (none)")
            for b in res["backups"]:
                out(f"  {b['backup_id']}  {b['created_at']}  {b['file_count']} files  {_mb(b['total_bytes'])}"
                    + (f"  - {b['note']}" if b.get("note") else ""))
            for u in res["unusable"]:
                out(f"  NOT USABLE  {u['folder']}: {u['problem']}")
            return 0
        if args.command == "show-backup":
            info = bk.show_backup(settings, args.backup_id)
            out(f"{info['backup_id']}  created {info['created_at']}  app {info['app_version']}  "
                f"{info['file_count']} files  {_mb(info['total_bytes'])}")
            for name, sec in info["sections"].items():
                out(f"  {name:<40} {sec['files']:>6} files  {_mb(sec['bytes'])}")
            out(f"  excluded: {info['excluded_count']} item(s) (secrets, temporary files, caches)")
            return 0
        if args.command == "verify-backup":
            r = bk.verify_backup(settings, args.backup_id)
            audit.record("backup_verified", actor=CLI_ACTOR, target=args.backup_id,
                         details={"valid": r.valid, "files_checked": r.files_checked, "problems": len(r.problems)})
            return _finish_verify(settings, "data", args, r, out)
        if args.command == "restore-backup":
            res = bk.restore_backup(settings, args.backup_id, args.to)
            audit.record("backup_restored", actor=CLI_ACTOR, target=args.backup_id,
                         details={"destination": res["destination"], "file_count": res["file_count"]})
            out(f"Restored {res['file_count']} files from {res['backup_id']} into {res['destination']} (verified).")
            out("Production data was not changed. See RESTORE_README.txt in that folder to use the restored copy.")
            return 0
    except bk.BackupError as exc:
        audit.record("backup_failed", actor=CLI_ACTOR, target=getattr(args, "backup_id", None),
                     details={"command": args.command, "reason": str(exc)[:300]})
        out(f"Error: {exc}")
        return 1
    return 2


def source_backup_command(settings: Settings, args, out=print) -> int:
    """Phase 14.1 -- SOURCE backups (application code + recovery docs). Separate from data backups."""
    from . import backup as bk
    from . import source_backup as sb

    audit = AuditLog(settings.resolved_auth_dir)
    try:
        if args.command == "create-source-backup":
            out(f"Creating a SOURCE backup in {sb.source_backup_dir(settings)} (project files are only read) ...")
            m = sb.create_source_backup(settings, note=args.note)
            audit.record("source_backup_created", actor=CLI_ACTOR, target=m["backup_id"],
                         details={"file_count": m["file_count"], "total_bytes": m["total_bytes"], "excluded": len(m["excluded"])})
            out(f"Source backup {m['backup_id']} created and verified: {m['file_count']} files, {_mb(m['total_bytes'])}, "
                f"{len(m['excluded'])} excluded item(s).")
            if m["missing_allowlisted"]:
                out(f"Not present (skipped): {', '.join(m['missing_allowlisted'])}")
            out("This backs up code only. Data needs create-backup. Copy both to another physical drive.")
            return 0
        if args.command == "list-source-backups":
            res = sb.list_source_backups(settings)
            out(f"Source backups in {res['backup_dir']}:")
            if not res["backups"]:
                out("  (none)")
            for b in res["backups"]:
                out(f"  {b['backup_id']}  {b['created_at']}  {b['file_count']} files  {_mb(b['total_bytes'])}  {b['status']}"
                    + (f"  - {b['note']}" if b.get("note") else ""))
            for u in res["unusable"]:
                out(f"  NOT USABLE  {u['folder']}: {u['problem']}")
            return 0
        if args.command == "verify-source-backup":
            r = sb.verify_source_backup(settings, args.backup_id)
            audit.record("source_backup_verified", actor=CLI_ACTOR, target=args.backup_id,
                         details={"valid": r.valid, "files_checked": r.files_checked, "problems": len(r.problems)})
            return _finish_verify(settings, "source", args, r, out)
    except bk.BackupError as exc:
        audit.record("source_backup_failed", actor=CLI_ACTOR, target=getattr(args, "backup_id", None),
                     details={"command": args.command, "reason": str(exc)[:300]})
        out(f"Error: {exc}")
        return 1
    return 2


def recovery_backup_command(settings: Settings, args, out=print) -> int:
    """Phase 14.4.1 -- RECOVERY-ARTEFACT backups (automation/backups + automation/.state). Separate from data and
    source backups."""
    from . import backup as bk
    from . import recovery_backup as rb

    audit = AuditLog(settings.resolved_auth_dir)
    try:
        if args.command == "create-recovery-backup":
            out(f"Creating a RECOVERY-ARTEFACT backup in {rb.recovery_backup_dir(settings)} (sources are only read) ...")
            m = rb.create_recovery_backup(settings, note=args.note)
            audit.record("recovery_backup_created", actor=CLI_ACTOR, target=m["backup_id"],
                         details={"file_count": m["file_count"], "total_bytes": m["total_bytes"], "excluded": len(m["excluded"])})
            out(f"Recovery backup {m['backup_id']} created and verified: {m['file_count']} files, {_mb(m['total_bytes'])}, "
                f"{len(m['excluded'])} excluded item(s).")
            if m["missing_allowlisted"]:
                out(f"Not present (skipped): {', '.join(m['missing_allowlisted'])}")
            out("This covers automation recovery artefacts only. Copy it to another physical drive.")
            return 0
        if args.command == "list-recovery-backups":
            res = rb.list_recovery_backups(settings)
            out(f"Recovery backups in {res['backup_dir']}:")
            if not res["backups"]:
                out("  (none)")
            for b in res["backups"]:
                out(f"  {b['backup_id']}  {b['created_at']}  {b['file_count']} files  {_mb(b['total_bytes'])}  {b['status']}"
                    + (f"  - {b['note']}" if b.get("note") else ""))
            for u in res["unusable"]:
                out(f"  NOT USABLE  {u['folder']}: {u['problem']}")
            return 0
        if args.command == "verify-recovery-backup":
            r = rb.verify_recovery_backup(settings, args.backup_id)
            audit.record("recovery_backup_verified", actor=CLI_ACTOR, target=args.backup_id,
                         details={"valid": r.valid, "files_checked": r.files_checked, "problems": len(r.problems)})
            return _finish_verify(settings, "recovery", args, r, out)
    except bk.BackupError as exc:
        audit.record("recovery_backup_failed", actor=CLI_ACTOR, target=getattr(args, "backup_id", None),
                     details={"command": args.command, "reason": str(exc)[:300]})
        out(f"Error: {exc}")
        return 1
    return 2


def offline_copy_command(settings: Settings, args, out=print) -> int:
    """Phase 14.6 -- copy-backups-offline --to <path>. Exit 0 only when the copy was verified and finalized."""
    from . import backup as bk
    from . import offline_copy as oc

    audit = AuditLog(settings.resolved_auth_dir)
    try:
        out(f"Offline copy of {settings.resolved_backup_dir} -> {args.to} (the source is only read) ...")
        s = oc.copy_backups_offline(settings, args.to, log=out)
        audit.record("offline_copy_completed", actor=CLI_ACTOR, target=s["destination"],
                     details={"backups": len(s["backups"]), "file_count": s["file_count"], "total_bytes": s["total_bytes"],
                              "disk": s["disk"].get("name")})
        out(f"OFFLINE COPY COMPLETE and VERIFIED: {s['destination']}")
        out(f"  {len(s['backups'])} backups, {s['reports_copied']} earlier report(s), {s['file_count']} files, "
            f"{_mb(s['total_bytes'])}; verification reports in Backup Reports\\")
        for item in s["skipped"]:
            out(f"  skipped (not copied): {item['path']} - {item['reason']}")
        out("Now use 'Safely Remove Hardware', unplug the drive and store it away from this computer.")
        return 0
    except (bk.BackupError, OSError) as exc:
        audit.record("offline_copy_failed", actor=CLI_ACTOR, target=str(args.to), details={"reason": str(exc)[:300]})
        out(f"Error: {exc}")
        out("OFFLINE COPY FAILED. Any '.incomplete' folder on the destination is NOT a usable backup "
            "(see its OFFLINE_COPY_STATUS.txt). Nothing was deleted.")
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Synergy Vessel dashboard administration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="create an administrator account interactively")
    sub.add_parser("verify-audit", help="verify the audit log hash chain")
    p = sub.add_parser("create-backup", help="back up historical data and dashboard data")
    p.add_argument("--note", default=None, help="short description stored with the backup")
    sub.add_parser("list-backups", help="list complete backups (and unusable ones)")
    sub.add_parser("show-backup", help="show a backup's metadata").add_argument("backup_id")
    _verify_parser(sub, "verify-backup", "re-hash every file of a backup")
    p = sub.add_parser("restore-backup", help="restore a verified backup into a NEW or EMPTY folder (never production)")
    p.add_argument("backup_id")
    p.add_argument("--to", required=True, type=Path, help="absolute path of a new or empty destination folder")
    p = sub.add_parser("create-source-backup", help="back up the application SOURCE code (not data)")
    p.add_argument("--note", default=None, help="short description stored with the source backup")
    sub.add_parser("list-source-backups", help="list source backups (and unusable ones)")
    _verify_parser(sub, "verify-source-backup", "re-hash every file of a source backup")
    p = sub.add_parser("create-recovery-backup", help="back up automation recovery artefacts (automation/backups, .state)")
    p.add_argument("--note", default=None, help="short description stored with the recovery backup")
    sub.add_parser("list-recovery-backups", help="list recovery-artefact backups (and unusable ones)")
    _verify_parser(sub, "verify-recovery-backup", "re-hash every file of a recovery-artefact backup")
    p = sub.add_parser("copy-backups-offline",
                       help="copy + verify the whole backup collection onto a SEPARATE physical drive (manual, never mirrors)")
    p.add_argument("--to", required=True, type=Path, help="absolute path on the external drive, e.g. F:\\")
    p = sub.add_parser("generate-viewpoints", help="SV-08: propose the viewpoint mapping (nothing approved automatically)")
    p.add_argument("--dry-run", action="store_true", help="only print the summary")
    p.add_argument("--out", type=Path, default=None, help="write to this folder instead of the viewpoint folder (no audit entry)")
    p.add_argument("--replace-proposals", action="store_true",
                   help="replace an existing mapping that has no administrator decisions")
    args = parser.parse_args(argv)
    settings = get_settings()
    auth_dir = settings.resolved_auth_dir
    if args.command in ("create-source-backup", "list-source-backups", "verify-source-backup"):
        return source_backup_command(settings, args)
    if args.command in ("create-recovery-backup", "list-recovery-backups", "verify-recovery-backup"):
        return recovery_backup_command(settings, args)
    if args.command == "generate-viewpoints":
        return generate_viewpoints_command(settings, args)
    if args.command == "copy-backups-offline":
        return offline_copy_command(settings, args)
    if args.command.endswith(("-backup", "-backups")):
        return backup_command(settings, args)
    if args.command == "create-admin":
        if not sys.stdin.isatty():
            print("create-admin must be run in an interactive terminal.")
            return 2
        try:
            return create_admin(auth_dir)
        except (EOFError, KeyboardInterrupt):
            print()
            print("Cancelled. No account was created.")
            return 130
    return verify_audit(auth_dir)


if __name__ == "__main__":
    sys.exit(main())
