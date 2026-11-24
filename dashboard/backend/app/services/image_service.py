"""Image indexing and safe path resolution.

Image IDs are the file stems in each week's ``Extracted Images - New`` folder
(e.g. ``W30-0001``). Requests are resolved only through the in-memory index
built at startup, never by joining user input onto a path, and the resolved
path is re-checked to sit inside an approved image folder.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

IMAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


class InvalidImageId(ValueError):
    pass


class ImageNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ImageFile:
    image_id: str
    path: Path
    week_folder: str
    sha256: str
    size_bytes: int


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_image_dir(image_dir: Path, week_folder: str, suffixes: tuple[str, ...]) -> list[ImageFile]:
    if not image_dir.is_dir():
        return []
    out = []
    for p in sorted(image_dir.iterdir(), key=lambda q: q.name):
        if p.is_file() and p.suffix.lower() in suffixes:
            out.append(ImageFile(p.stem, p.resolve(), week_folder, sha256_file(p), p.stat().st_size))
    return out


class ImageIndex:
    def __init__(self, approved_dirs: list[Path]):
        self._approved = [d.resolve() for d in approved_dirs]
        self._by_id: dict[str, ImageFile] = {}
        self.duplicate_ids: list[str] = []

    def approve(self, folder: Path) -> None:
        self._approved.append(folder.resolve())

    def add(self, img: ImageFile) -> None:
        if img.image_id in self._by_id:
            self.duplicate_ids.append(img.image_id)
            return
        self._by_id[img.image_id] = img

    def __contains__(self, image_id: str) -> bool:
        return image_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, image_id: str) -> Optional[ImageFile]:
        return self._by_id.get(image_id)

    def resolve(self, image_id: str) -> tuple[Path, str]:
        """Return (path, media_type) or raise InvalidImageId / ImageNotFound."""
        if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
            raise InvalidImageId("invalid image identifier")
        img = self._by_id.get(image_id)
        if img is None:
            raise ImageNotFound(f"unknown image id '{image_id}'")
        path = img.path.resolve()
        if not any(path.is_relative_to(d) for d in self._approved):
            raise ImageNotFound("image is outside the approved folders")
        if not path.is_file():
            raise ImageNotFound(f"image file for '{image_id}' is missing on disk")
        media = MEDIA_TYPES.get(path.suffix.lower())
        if media is None:
            raise ImageNotFound("unsupported image type")
        return path, media
