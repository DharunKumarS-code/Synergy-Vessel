"""Extraction-only CLI: extract every embedded image from the historical weekly source
workbooks and write one report per week:

    <week folder>/Week_NN_Image_Register.xlsx   (one worksheet, "Image Register", 9 columns)
    <week folder>/Extracted Images - New/WNN-0001.jpg, WNN-0002.jpg, ...

    python extract_images.py                 # every week folder under the project root
    python extract_images.py --weeks 30 31

Re-runs are safe: a week that is already complete (its register and photos validate) is
skipped untouched; a week with partial or inconsistent output is reported as needing attention
and left as it is; only weeks with no output yet are extracted. --weeks limits the selection.
The week's original "Extracted Images" folder is never written to. A file name that already exists is never overwritten: the new
file gets a "_v2" (v3, ...) suffix, the register records that name, and the collision is reported.

No AI classification. Never loads .env, never initializes Gemini/Groq, never touches the network
(outbound socket connections are blocked for the whole run as a guard), never modifies source
workbooks or existing "Extracted Images" folders, and never overwrites an existing file.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

AUTOMATION_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(AUTOMATION_ROOT))

from src import image_extraction as ie  # noqa: E402
from src import weekly_image_report as wr  # noqa: E402

DEFAULT_PROJECT_ROOT = AUTOMATION_ROOT.parent


class NetworkBlockedError(RuntimeError):
    pass


def block_network() -> None:
    """Belt-and-braces: this workflow needs no network, so any outbound connection fails."""
    def refuse(*_a, **_k):
        raise NetworkBlockedError("network access is disabled for the extraction-only workflow")
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--weeks", type=int, nargs="+", help="limit to these week numbers")
    parser.add_argument("--min-merge-width", type=int, default=ie.DEFAULT_MIN_MERGE_WIDTH)
    args = parser.parse_args(argv)

    block_network()
    run, outputs, statuses = wr.generate_weekly_reports(args.project_root, args.min_merge_width,
                                                        set(args.weeks) if args.weeks else None)
    problems = {o.week_number: wr.verify_week_output(o) for o in outputs}
    skipped = [s.week_number for s in statuses if s.status == wr.WEEK_COMPLETE]
    attention = [s for s in statuses if s.status == wr.WEEK_ATTENTION]

    print("Weekly image extraction (no AI classification performed)")
    print(f"  Weeks processed: {', '.join(str(o.week_number) for o in outputs) or 'none'}")
    print(f"  Weeks skipped as already complete: {', '.join(map(str, skipped)) or 'none'}")
    print(f"  Weeks requiring attention: {', '.join(str(s.week_number) for s in attention) or 'none'}")
    for s in attention:
        for reason in s.reasons:
            print(f"    Week {s.week_number} ({s.folder_name}): {reason}")
    print(f"  Photos extracted: {sum(1 for o in outputs for r in o.records if r.image_filename)}")
    if not outputs:
        return 1 if attention else 0
    for k, v in ie.summary_rows(run):
        print(f"  {k}: {v}")
    print("  Weekly outputs:")
    for o in outputs:
        status = "verified" if not problems[o.week_number] else "PROBLEMS: " + "; ".join(problems[o.week_number])
        filled = sum(1 for v in o.locations.values() if v)
        print(f"    Week {o.week_number}: {len(o.records)} rows, Official Location {filled} filled / "
              f"{len(o.records) - filled} blank -> {o.report_path} [{status}]")
    shown = [i for i in run.issues if i[0] != "DATE_DETAIL"]
    print(f"  Issues: {len(shown)}")
    for cat, where, detail in shown:
        print(f"    [{cat}] {where}: {detail}")
    bad = (attention or run.failed or any(w.error for w in run.workbooks)
           or not all(w.unchanged for w in run.workbooks) or any(problems.values()))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
