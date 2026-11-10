"""Pixel grid helpers: walk animation, sprite sheet assembly, default humanoid character.

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


# ---------------------------------------------------------------- default character
# Index 0 is transparent. Each ramp runs light, mid, dark.
DEFAULT_PALETTE = [
    "transparent",
    "#2b2430",  # 1 outline, eyes
    "#a06c40", "#7f5230", "#5e3a22",  # 2-4 hair
    "#f4cfa8", "#e2b088", "#c8916c",  # 5-7 skin
    "#5d93d6", "#4072b4", "#2f5592",  # 8-10 shirt
    "#969aa4", "#767a85", "#5a5d68",  # 11-13 trousers
    "#6b4e3b", "#4d3729",  # 14-15 shoes
]
HAIR, SKIN, SHIRT, TROUSERS, SHOES = (2, 3, 4), (5, 6, 7), (8, 9, 10), (11, 12, 13), (14, 14, 15)
OUTLINE = 1
FLOOR = 29  # lowest row of the feet
HIP = 22  # first row of the legs when the body is not raised

Pixels = set[tuple[int, int]]


def _rect(x0: int, y0: int, x1: int, y1: int) -> Pixels:
    return {(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)}


def _ellipse(cx: float, cy: float, rx: float, ry: float) -> Pixels:
    return {
        (x, y)
        for y in range(CELL)
        for x in range(CELL)
        if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0
    }


def _limb(x: int, width: int, top: int, bottom: int, lean: int = 0) -> Pixels:
    """Vertical bar whose lower end is shifted sideways by `lean` pixels."""
    span = max(1, bottom - top)
    return {
        (x + round(lean * (y - top) / span) + dx, y)
        for y in range(top, bottom + 1)
        for dx in range(width)
    }


def _shade(pixels: Pixels, x: int, y: int, ramp: tuple[int, int, int]) -> int:
    """Light comes from the upper left, so the right side of each part is darkest."""
    xs, ys = [p[0] for p in pixels], [p[1] for p in pixels]
    t = 0.8 * (x - min(xs)) / max(1, max(xs) - min(xs)) + 0.2 * (y - min(ys)) / max(1, max(ys) - min(ys))
    return ramp[2] if t > 0.82 else ramp[1] if t > 0.6 else ramp[0]


def _paint(g: Grid, pixels: Pixels, ramp: tuple[int, int, int]) -> None:
    """Fill a body part with a 1px outline."""
    for x, y in pixels:
        if any((x + dx, y + dy) not in pixels for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
            g[y][x] = OUTLINE
        else:
            g[y][x] = _shade(pixels, x, y, ramp)


def _recolor(g: Grid, pixels: Pixels, ramp: tuple[int, int, int]) -> None:
    """Change the fill inside an already painted part, keeping its outline."""
    for x, y in pixels:
        if g[y][x] not in (0, OUTLINE):
            g[y][x] = _shade(pixels, x, y, ramp)


def _in_shadow(ramp: tuple[int, int, int]) -> tuple[int, int, int]:
    """The same color one step darker, for limbs on the far side of the body."""
    return ramp[1], ramp[2], ramp[2]


def _leg(g: Grid, x: int, width: int, top: int, foot: int, lean: int = 0, toe: int = 0, far: bool = False) -> None:
    """Trouser leg ending in a shoe whose sole is on row `foot`. toe: extra length to the right."""
    trousers = _in_shadow(TROUSERS) if far else TROUSERS
    _paint(g, _limb(x, width, top, foot - 1, lean), trousers)
    _paint(g, _rect(x + lean, foot - 2, x + lean + width - 1 + toe, foot), SHOES)


def _arm(g: Grid, x: int, width: int, top: int, bottom: int, lean: int = 0, far: bool = False) -> None:
    """Sleeve ending in a 2-row hand."""
    arm = _limb(x, width, top, bottom, lean)
    _paint(g, arm, _in_shadow(SHIRT) if far else SHIRT)
    _recolor(g, {p for p in arm if p[1] >= bottom - 1}, _in_shadow(SKIN) if far else SKIN)


def _front_or_back(view: str, stride: int) -> Grid:
    g = empty_grid()
    y = -1 if stride else 0  # the body rises 1px on each step
    # One leg lifts 2px while the other stays on the floor. The opposite arm swings forward.
    _leg(g, 11, 5, HIP + y, FLOOR - 2 if stride > 0 else FLOOR)
    _leg(g, 16, 5, HIP + y, FLOOR - 2 if stride < 0 else FLOOR)
    _arm(g, 7, 4, 16 + y, 22 + y + 2 * stride)
    _arm(g, 21, 4, 16 + y, 22 + y - 2 * stride)
    _paint(g, _rect(10, 15 + y, 21, 22 + y), SHIRT)

    head = _ellipse(15.5, 9.5 + y, 8.8, 7.1)
    _paint(g, head, SKIN)
    if view == "back":
        _recolor(g, head, HAIR)
        return g

    hair = {p for p in head if p[1] <= 7 + y}  # top of the head
    hair |= {p for p in head if p[1] <= 11 + y and (p[0] <= 8 or p[0] >= 23)}  # sides
    hair |= {(x, 8 + y) for x in range(9, 23) if x % 3 != 0}  # uneven fringe
    _recolor(g, hair, HAIR)
    for x in (11, 12, 19, 20):
        g[11 + y][x] = g[12 + y][x] = OUTLINE
    g[14 + y][15] = g[14 + y][16] = SKIN[2]
    return g


def _right(stride: int) -> Grid:
    g = empty_grid()
    y = -1 if stride else 0
    near = 3 * stride  # near leg swings forward on +1, back on -1
    swing = 4 * stride
    # Far side first, so that the near limbs are drawn over it. The arm on each side
    # swings against the leg on that side, and the leg moving backward lifts its heel.
    _arm(g, 14, 4, 16 + y, 22 + y, lean=swing, far=True)
    _leg(g, 14, 4, HIP + y, FLOOR - (1 if stride > 0 else 0), lean=-near, toe=2, far=True)
    _leg(g, 14, 4, HIP + y, FLOOR - (1 if stride < 0 else 0), lean=near, toe=2)
    _paint(g, _rect(12, 15 + y, 19, 22 + y), SHIRT)
    _arm(g, 14, 4, 16 + y, 22 + y, lean=-swing)

    head = _ellipse(15.5, 9.5 + y, 8.8, 7.1) | _rect(24, 11 + y, 24, 13 + y) | {(25, 12 + y)}  # nose
    _paint(g, head, SKIN)
    hair = {p for p in head if p[1] <= 7 + y or p[0] <= 14}  # top and back of the head
    hair |= {(x, 8 + y) for x in range(15, 24) if x % 3 != 0}  # uneven fringe
    _recolor(g, hair, HAIR)
    for ear_y in (10, 11, 12):
        g[ear_y + y][13] = g[ear_y + y][14] = SKIN[1]
    for x in (20, 21):
        g[11 + y][x] = g[12 + y][x] = OUTLINE
    g[14 + y][22] = g[14 + y][23] = SKIN[2]
    return g


def default_sheet() -> Image.Image:
    """Humanoid template, drawn frame by frame for a real walk cycle.

    walk_frames() only moves one still pose, which is all a generated character has.
    Frame timing matches it: 0 and 2 stand on the floor, 1 and 3 are raised 1px.
    """
    strides = (0, 1, 0, -1)
    rows = {
        "down": [_front_or_back("front", s) for s in strides],
        "left": [flip_h(_right(s)) for s in strides],
        "right": [_right(s) for s in strides],
        "up": [_front_or_back("back", s) for s in strides],
    }
    sheet = Image.new("RGBA", (CELL * COLS, CELL * len(ROW_ORDER)), (0, 0, 0, 0))
    for r, direction in enumerate(ROW_ORDER):
        for c, frame in enumerate(rows[direction]):
            sheet.paste(grid_to_image(frame, DEFAULT_PALETTE), (c * CELL, r * CELL))
    return sheet
