"""Generate icon.png and icon.ico using only Python stdlib.

Draws a 32x32 rounded-square "G" badge so the launcher can ship a custom
icon without bundling a binary asset.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 32
BG = (32, 78, 156, 255)        # deep blue
FG = (240, 244, 255, 255)      # near-white
ACCENT = (90, 198, 255, 255)   # cyan accent

OUT_DIR = Path(__file__).resolve().parent
PNG_PATH = OUT_DIR / "icon.png"
ICO_PATH = OUT_DIR / "icon.ico"


def make_canvas(size: int) -> list[list[tuple[int, int, int, int]]]:
    return [[(0, 0, 0, 0) for _ in range(size)] for _ in range(size)]


def fill_rounded_rect(canvas, x0, y0, x1, y1, radius, color) -> None:
    for y in range(y0, y1):
        for x in range(x0, x1):
            in_corner = False
            cx = cy = 0
            if x < x0 + radius and y < y0 + radius:
                cx, cy, in_corner = x0 + radius, y0 + radius, True
            elif x >= x1 - radius and y < y0 + radius:
                cx, cy, in_corner = x1 - radius - 1, y0 + radius, True
            elif x < x0 + radius and y >= y1 - radius:
                cx, cy, in_corner = x0 + radius, y1 - radius - 1, True
            elif x >= x1 - radius and y >= y1 - radius:
                cx, cy, in_corner = x1 - radius - 1, y1 - radius - 1, True
            if in_corner and (x - cx) ** 2 + (y - cy) ** 2 > radius * radius:
                continue
            canvas[y][x] = color


def stroke_circle(canvas, cx, cy, r, color, thickness=2) -> None:
    inner = (r - thickness) ** 2
    outer = r * r
    for y in range(cy - r - 1, cy + r + 2):
        for x in range(cx - r - 1, cx + r + 2):
            if 0 <= x < SIZE and 0 <= y < SIZE:
                d = (x - cx) ** 2 + (y - cy) ** 2
                if inner <= d <= outer:
                    canvas[y][x] = color


def fill_rect(canvas, x0, y0, x1, y1, color) -> None:
    for y in range(max(0, y0), min(SIZE, y1)):
        for x in range(max(0, x0), min(SIZE, x1)):
            canvas[y][x] = color


def draw_letter_g(canvas) -> None:
    cx, cy, r = 16, 16, 9
    stroke_circle(canvas, cx, cy, r, FG, thickness=2)
    # carve mouth on the right
    fill_rect(canvas, cx + 2, cy - 2, cx + r + 2, cy + 2, (0, 0, 0, 0))
    # crossbar
    fill_rect(canvas, cx, cy, cx + r, cy + 2, FG)
    fill_rect(canvas, cx + r - 2, cy, cx + r, cy + 5, FG)
    # accent dot (top-right)
    stroke_circle(canvas, 25, 7, 2, ACCENT, thickness=2)


def render_canvas() -> bytes:
    canvas = make_canvas(SIZE)
    fill_rounded_rect(canvas, 0, 0, SIZE, SIZE, 6, BG)
    draw_letter_g(canvas)
    raw = bytearray()
    for row in canvas:
        raw.append(0)  # PNG filter byte (none)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    return bytes(raw)


def write_png(raw_rgba_with_filters: bytes, path: Path) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw_rgba_with_filters, 9)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    path.write_bytes(png)


def write_ico_from_png(png_path: Path, ico_path: Path) -> None:
    """Wrap the PNG bytes in an ICO container (Vista+ supports embedded PNG)."""
    png_bytes = png_path.read_bytes()
    icondir = struct.pack("<HHH", 0, 1, 1)
    width = SIZE if SIZE < 256 else 0
    height = SIZE if SIZE < 256 else 0
    entry = struct.pack(
        "<BBBBHHII",
        width,
        height,
        0,    # color count
        0,    # reserved
        1,    # planes
        32,   # bpp
        len(png_bytes),
        6 + 16,
    )
    ico_path.write_bytes(icondir + entry + png_bytes)


def main() -> int:
    raw = render_canvas()
    write_png(raw, PNG_PATH)
    write_ico_from_png(PNG_PATH, ICO_PATH)
    print(f"wrote {PNG_PATH}")
    print(f"wrote {ICO_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
