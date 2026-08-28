"""Regenerates the tray icons and icon.ico without any image library.

Run from the project root:  python assets/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent

IDLE = (108, 132, 168)
RECORDING = (214, 68, 62)
PROCESSING = (222, 158, 54)
WHITE = (255, 255, 255)


def draw(size: int, color: tuple[int, int, int]) -> bytes:
    """RGBA rows: a filled disc with a simple microphone glyph on top."""
    rows = []
    center = (size - 1) / 2.0
    radius = size / 2.0 - size * 0.02
    cap_w = size * 0.22
    cap_top = size * 0.24
    cap_bottom = size * 0.56
    cap_radius = cap_w / 2.0
    arc_r_out = size * 0.30
    arc_r_in = size * 0.24
    stand_top = size * 0.70
    stand_w = size * 0.06
    base_y = size * 0.82
    base_w = size * 0.30

    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx = x - center
            dy = y - center
            inside = dx * dx + dy * dy <= radius * radius
            if not inside:
                row += bytes((0, 0, 0, 0))
                continue

            pixel = color
            gy = y
            # microphone capsule
            if cap_top <= gy <= cap_bottom and abs(dx) <= cap_radius:
                pixel = WHITE
            elif gy < cap_top and (dx * dx + (gy - cap_top) ** 2) <= cap_radius ** 2:
                pixel = WHITE
            elif gy > cap_bottom and (dx * dx + (gy - cap_bottom) ** 2) <= cap_radius ** 2:
                pixel = WHITE
            # holder arc
            elif gy >= center * 0.95 and gy <= stand_top:
                d = (dx * dx + (gy - center) ** 2) ** 0.5
                if arc_r_in <= d <= arc_r_out:
                    pixel = WHITE
            # stand and base
            if stand_top <= gy <= base_y and abs(dx) <= stand_w / 2:
                pixel = WHITE
            if base_y <= gy <= base_y + size * 0.06 and abs(dx) <= base_w / 2:
                pixel = WHITE

            row += bytes((pixel[0], pixel[1], pixel[2], 255))
        rows.append(bytes(row))
    return b"".join(b"\x00" + row for row in rows)


def png_bytes(size: int, color: tuple[int, int, int]) -> bytes:
    raw = draw(size, color)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(color: tuple[int, int, int], sizes=(16, 32, 48, 64, 128)) -> bytes:
    """Vista-era ICO with PNG-compressed images inside."""
    images = [png_bytes(size, color) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32, len(data), offset,
        )
        offset += len(data)
    return header + entries + b"".join(images)


def main() -> None:
    (ASSETS / "icon_idle.png").write_bytes(png_bytes(128, IDLE))
    (ASSETS / "icon_recording.png").write_bytes(png_bytes(128, RECORDING))
    (ASSETS / "icon_processing.png").write_bytes(png_bytes(128, PROCESSING))
    (ASSETS / "icon.ico").write_bytes(ico_bytes(IDLE))
    print("Icons written to", ASSETS)


if __name__ == "__main__":
    main()
