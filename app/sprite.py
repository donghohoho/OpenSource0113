"""Pixel grid helpers: walk animation, sprite sheet assembly, default blob character.

A "grid" is a 32x32 list of lists of palette indices. Index 0 is transparent.
Sheet layout (matches reference/default_sheet.png):
    rows    : down, left, right, up
    columns : 4 walk frames
    cell    : 32x32 px, whole sheet 128x128 px
"""
from __future__ import annotations

import io

from PIL import Image

CELL = 32
COLS = 4
ROW_ORDER = ("down", "left", "right", "up")

Grid = list[list[int]]


# ---------------------------------------------------------------- grid ops
def empty_grid() -> Grid:
    return [[0] * CELL for _ in range(CELL)]


def flip_h(grid: Grid) -> Grid:
    return [list(reversed(row)) for row in grid]


def shift(grid: Grid, dx: int = 0, dy: int = 0) -> Grid:
    """Move the whole picture; pixels pushed off the edge are dropped."""
    out = empty_grid()
    for y in range(CELL):
        for x in range(CELL):
            v = grid[y][x]
            nx, ny = x + dx, y + dy
            if v and 0 <= nx < CELL and 0 <= ny < CELL:
                out[ny][nx] = v
    return out


def shift_rows(grid: Grid, y0: int, y1: int, dx: int) -> Grid:
    """Horizontally shift only rows y0..y1 (inclusive)."""
    out = [row[:] for row in grid]
    for y in range(max(0, y0), min(CELL - 1, y1) + 1):
        src = grid[y]
        new = [0] * CELL
        for x in range(CELL):
            nx = x + dx
            if src[x] and 0 <= nx < CELL:
                new[nx] = src[x]
        out[y] = new
    return out


def content_bottom(grid: Grid) -> int:
    for y in range(CELL - 1, -1, -1):
        if any(grid[y]):
            return y
    return CELL - 1


def walk_frames(base: Grid) -> list[Grid]:
    """Make a 4-frame walk cycle from one still pose, purely in code.

    0: base pose
    1: body bobs up 1px, feet (bottom 3 rows) lean left 1px
    2: base pose
    3: body bobs up 1px, feet lean right 1px
    """
    bottom = content_bottom(base)
    feet_top = bottom - 2

    def step(lean: int) -> Grid:
        g = shift(base, dy=-1)
        # after the bob the feet band moved up by one row as well
        return shift_rows(g, feet_top - 1, bottom - 1, lean)

    return [base, step(-1), [r[:] for r in base], step(1)]


# ---------------------------------------------------------------- rendering
def hex_to_rgba(color: str) -> tuple[int, int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255


def grid_to_image(grid: Grid, palette: list[str]) -> Image.Image:
    rgba = [(0, 0, 0, 0)] + [hex_to_rgba(c) for c in palette[1:]]
    img = Image.new("RGBA", (CELL, CELL), (0, 0, 0, 0))
    px = img.load()
    for y in range(CELL):
        for x in range(CELL):
            v = grid[y][x]
            if v:
                px[x, y] = rgba[v]
    return img


def build_sheet(front: Grid, back: Grid, right: Grid, palette: list[str]) -> Image.Image:
    """front=facing down, back=facing up, right=facing right. Left is right mirrored."""
    bases = {"down": front, "left": flip_h(right), "right": right, "up": back}
    sheet = Image.new("RGBA", (CELL * COLS, CELL * len(ROW_ORDER)), (0, 0, 0, 0))
    for r, direction in enumerate(ROW_ORDER):
        for c, frame in enumerate(walk_frames(bases[direction])):
            sheet.paste(grid_to_image(frame, palette), (c * CELL, r * CELL))
    return sheet


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- default blob
BLOB_PALETTE = [
    "transparent",
    "#2a2433",  # 1 outline (dark)
    "#ffffff",  # 2 body white
    "#dfe3ec",  # 3 light gray shade
    "#b9bfcf",  # 4 deeper shade
    "#14121a",  # 5 eye black
]


def blob_frame(view: str) -> Grid:
    """Squat white blob, head+body in one. view: 'front' | 'back' | 'right'."""
    cx, cy, rx, ry, floor_y = 15.5, 18.5, 12.1, 10.8, 27
    inside = [[False] * CELL for _ in range(CELL)]
    for y in range(CELL):
        for x in range(CELL):
            if y <= floor_y and ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                inside[y][x] = True

    def is_in(x: int, y: int) -> bool:
        return 0 <= x < CELL and 0 <= y < CELL and inside[y][x]

    g = empty_grid()
    for y in range(CELL):
        for x in range(CELL):
            if not inside[y][x]:
                continue
            edge = not all(is_in(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            if edge:
                g[y][x] = 1
                continue
            # light from upper-left: rounded falloff toward the lower-right rim
            d = (((x - (cx - 3.5)) / rx) ** 2 + ((y - (cy - 4.5)) / ry) ** 2) ** 0.5
            g[y][x] = 4 if d > 1.18 else 3 if d > 0.95 else 2

    eye_rows = (17, 18)
    if view == "front":
        eye_cols = (11, 20)
    elif view == "right":
        eye_cols = (22,)
    else:  # back: no eyes
        eye_cols = ()
    for ex in eye_cols:
        for ey in eye_rows:
            g[ey][ex] = 5
    return g


def default_sheet() -> Image.Image:
    return build_sheet(blob_frame("front"), blob_frame("back"), blob_frame("right"), BLOB_PALETTE)
