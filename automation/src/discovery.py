"""Find weekly folders and their source inspection workbooks under the project root."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

WEEK_DIR_RE = re.compile(r"^week\s*(\d+)$", re.IGNORECASE)
LOCK_FILE_PREFIX = "~$"
OUTPUT_FILE_PREFIX = "final "  # "Final Activity Report.xlsx", "Final ... Report.xlsx", etc.
# Extraction-only weekly register written by extract_images.py directly into the week folder
# ("Week_30_Image_Register.xlsx", or "..._v2.xlsx" after a name collision) -- generated, not a source.
GENERATED_REGISTER_RE = re.compile(r"^week_\d+_image_register(_v\d+)?\.xlsx$", re.IGNORECASE)


@dataclass
class WeekFolder:
    name: str
    path: Path
    source_workbooks: list[Path]


def find_week_folders(root: Path) -> list[WeekFolder]:
   
    found = []
    for entry in root.iterdir():
        match = WEEK_DIR_RE.match(entry.name.strip())
        if match and entry.is_dir():
            found.append((int(match.group(1)), entry.name.casefold(), entry.name, entry))
    return [WeekFolder(name=name, path=entry, source_workbooks=find_source_workbooks(entry))
            for _, _, name, entry in sorted(found, key=lambda t: t[:3])]


PART_NUMBER_RE = re.compile(r"\((\d+)\)\s*\.xlsx$", re.IGNORECASE)


def _part_sort_key(path: Path) -> tuple[int, str]:
    
    m = PART_NUMBER_RE.search(path.name)
    return (int(m.group(1)) if m else 10**6, path.name)


def find_source_workbooks(week_dir: Path) -> list[Path]:
   
    candidates = []
    for f in week_dir.glob("*.xlsx"):
        if not f.is_file():  # P2 remediation (defect 5): a folder named "x.xlsx" is not a workbook
            continue
        if f.name.startswith(LOCK_FILE_PREFIX):
            continue
        if f.name.lower().startswith(OUTPUT_FILE_PREFIX):
            continue
        if GENERATED_REGISTER_RE.match(f.name):
            continue
        candidates.append(f)
    return sorted(candidates, key=_part_sort_key)
