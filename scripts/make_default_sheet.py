"""Draw reference/default_sheet.png (128x128, 4 rows x 4 cols, 32x32 cells) in code.

Run from the project root:  python scripts/make_default_sheet.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.sprite import default_sheet  # noqa: E402

out = ROOT / "reference" / "default_sheet.png"
out.parent.mkdir(exist_ok=True)
sheet = default_sheet()
sheet.save(out)
print(f"saved {out.relative_to(ROOT)} size={sheet.size}")
