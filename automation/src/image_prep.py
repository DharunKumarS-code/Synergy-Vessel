"""Downscale a photo before sending it to the vision API -- classification only, never used
for the extracted-images folder or the embedded report photos, which stay full resolution.

Why: Gemini tokenizes images in fixed-size tiles (around 768x768); an image whose longer side
exceeds that gets split across multiple tiles and costs roughly proportionally more tokens.
This project's extracted photos are consistently 850x637 -- just over the single-tile
threshold. Visually confirmed (by direct inspection, not just measurement) that downscaling to
768px or even 512px on the long side does not affect legibility of the markings this pipeline
depends on (e.g. a "1S" hold marking stays clearly readable at both sizes).
"""

from __future__ import annotations

import io

from PIL import Image

DEFAULT_MAX_DIMENSION = 768


def resize_for_classification(image_bytes: bytes, max_dimension: int = DEFAULT_MAX_DIMENSION) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as im:
        if max(im.size) <= max_dimension:
            return image_bytes  # already small enough -- skip a pointless re-encode
        scale = max_dimension / max(im.size)
        new_size = (round(im.width * scale), round(im.height * scale))
        resized = im.convert("RGB").resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
