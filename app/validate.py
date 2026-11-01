"""Strict validation of the AI's pixel-art JSON.

Expected shape:
{
  "palette": ["transparent", "#RRGGBB", ...],          # 2..16 entries, index 0 = transparent
  "frames": {
    "front": ["<32 hex digits>", ... 32 rows],         # each digit = palette index (0-F)
    "back":  [...],
    "right": [...]
  }
}
"""
from __future__ import annotations

import json
import re

from .sprite import CELL, Grid

FRAME_KEYS = ("front", "back", "right")
MAX_COLORS = 16
MIN_FILLED_PIXELS = 40  # a frame with fewer opaque pixels is treated as empty/broken
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
ROW_RE = re.compile(r"^[0-9A-Fa-f]+$")


class FormatError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:8]))
        self.errors = errors


def extract_json(text: str) -> dict:
    """Parse the model output; tolerates a ```json fence but nothing else."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise FormatError(["response is not JSON (no object found)"])
        try:
            data = json.loads(s[start : end + 1])
        except json.JSONDecodeError as e:
            raise FormatError([f"response is not valid JSON: {e.msg} at pos {e.pos}"])
    if not isinstance(data, dict):
        raise FormatError(["top-level JSON value must be an object"])
    return data


def validate(data: dict) -> tuple[list[str], dict[str, Grid]]:
    """Return (palette, {front, back, right: grid}) or raise FormatError listing every problem."""
    errors: list[str] = []

    palette = data.get("palette")
    if not isinstance(palette, list):
        raise FormatError(["'palette' must be a list"])
    if not (2 <= len(palette) <= MAX_COLORS):
        errors.append(f"palette must have 2..{MAX_COLORS} entries, got {len(palette)}")
    if palette and not (isinstance(palette[0], str) and palette[0].lower() == "transparent"):
        errors.append("palette[0] must be the string \"transparent\"")
    for i, c in enumerate(palette[1:], start=1):
        if not (isinstance(c, str) and HEX_COLOR.match(c)):
            errors.append(f"palette[{i}] = {c!r} is not a #RRGGBB color")

    frames = data.get("frames")
    if not isinstance(frames, dict):
        raise FormatError(errors + ["'frames' must be an object with keys front, back, right"])

    grids: dict[str, Grid] = {}
    for key in FRAME_KEYS:
        rows = frames.get(key)
        if not isinstance(rows, list):
            errors.append(f"frames.{key} is missing or not a list")
            continue
        if len(rows) != CELL:
            errors.append(f"frames.{key} has {len(rows)} rows, must be exactly {CELL}")
        grid: Grid = []
        for y, row in enumerate(rows[:CELL]):
            if not isinstance(row, str) or not ROW_RE.match(row):
                errors.append(f"frames.{key}[{y}] must be a string of hex digits 0-F")
                grid.append([0] * CELL)
                continue
            if len(row) != CELL:
                errors.append(f"frames.{key}[{y}] has {len(row)} characters, must be exactly {CELL}")
            values = [int(ch, 16) for ch in row[:CELL]] + [0] * max(0, CELL - len(row))
            bad = sorted({v for v in values if v >= len(palette)})
            if bad:
                errors.append(f"frames.{key}[{y}] uses palette index {bad} but palette has {len(palette)} entries")
            grid.append(values)
        filled = sum(1 for r in grid for v in r if v)
        if len(grid) == CELL and filled < MIN_FILLED_PIXELS:
            errors.append(f"frames.{key} is almost empty ({filled} non-transparent pixels)")
        grids[key] = grid

    extra = set(frames) - set(FRAME_KEYS)
    if extra:
        errors.append(f"unexpected frame keys {sorted(extra)}; only front, back, right")

    if errors:
        raise FormatError(errors)
    return palette, grids
