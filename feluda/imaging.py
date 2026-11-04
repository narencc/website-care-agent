"""Perceptual image fingerprints for image and logo verification.

WordPress re-encodes, resizes and renames uploads, so byte hashes and URLs
cannot prove "the intended asset is shown". A fingerprint combines:

- a 64-bit difference hash (dHash) of the grayscale image: structure, robust
  to resizing and re-encoding;
- a 4x4 RGB thumbnail: colour, which dHash ignores (a teal and an orange
  version of the same logo have identical dHashes).

Two images match only when both the structure and the colour are close.
"""

from __future__ import annotations

import io

from PIL import Image

MATCH_MAX_DISTANCE = 10  # of 64 dHash bits: same picture after resize/re-encode
MATCH_MAX_COLOUR = 28  # mean absolute RGB difference (0-255) over the 4x4 thumbnail
SAME_AS_OLD_MAX_DISTANCE = 6
SAME_AS_OLD_MAX_COLOUR = 14


def _flatten(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def dhash(data: bytes, size: int = 8) -> int:
    img = _flatten(Image.open(io.BytesIO(data))).convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = img.tobytes()
    bits = 0
    for row in range(size):
        for col in range(size):
            left, right = px[row * (size + 1) + col], px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def fingerprint(data: bytes) -> str:
    """'<dhash>:<hex of a 4x4 RGB thumbnail>'."""
    thumb = _flatten(Image.open(io.BytesIO(data))).resize((4, 4), Image.Resampling.BOX).tobytes()
    return f"{dhash(data)}:{thumb.hex()}"


def compare(a: str, b: str) -> tuple[int, float]:
    """(structure distance in bits, mean colour difference 0-255)."""
    ha, ca = a.split(":")
    hb, cb = b.split(":")
    ba, bb = bytes.fromhex(ca), bytes.fromhex(cb)
    colour = sum(abs(x - y) for x, y in zip(ba, bb)) / max(1, len(ba))
    return bin(int(ha) ^ int(hb)).count("1"), round(colour, 1)


def matches(a: str, b: str) -> bool:
    d, c = compare(a, b)
    return d <= MATCH_MAX_DISTANCE and c <= MATCH_MAX_COLOUR


def same_as_old(a: str, b: str) -> bool:
    d, c = compare(a, b)
    return d <= SAME_AS_OLD_MAX_DISTANCE and c <= SAME_AS_OLD_MAX_COLOUR


def image_size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size
