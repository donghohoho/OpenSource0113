"""FastAPI server: serves the page and turns an uploaded photo into a 4x4 walk sprite sheet.

Run from the project root:  python -m uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import base64
import io
import logging
import threading

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError

from . import ai_client, config
from .sprite import build_sheet, to_png_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sprite.server")

app = FastAPI(title="AI Pixel Sprite Generator")
_generate_lock = threading.Lock()  # one generation at a time: no accidental parallel spending

MAX_PHOTO_SIDE = 768


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: str = ""):
        self.status, self.code, self.message, self.detail = status, code, message, detail


@app.exception_handler(AppError)
async def _app_error(_: Request, e: AppError):
    return JSONResponse(status_code=e.status, content={"error": e.code, "message": e.message, "detail": e.detail})


def _prepare_photo(raw: bytes) -> bytes:
    """Decode with Pillow (the real check, not the extension), normalise to a small RGB JPEG."""
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise AppError(400, "invalid_file", "이미지 파일을 읽을 수 없습니다. JPG, PNG, WEBP 이미지만 올려 주세요.")
    if fmt not in config.ALLOWED_FORMATS:
        raise AppError(400, "invalid_file", f"지원하지 않는 이미지 형식입니다 ({fmt}). JPG, PNG, WEBP만 가능합니다.")
    if min(img.size) < 32:
        raise AppError(400, "invalid_file", "이미지가 너무 작습니다. 가로·세로 32픽셀 이상인 사진을 올려 주세요.")

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg
    img = img.convert("RGB")
    img.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)  # re-encoding also drops EXIF metadata
    return buf.getvalue()


@app.get("/health")
def health():
    # Deploy check: which build is running. Makes no AI call and exposes no settings.
    return {"status": "ok", "version": config.APP_VERSION}


@app.get("/api/config")
def get_config():
    # Only whether a key exists, never the key itself.
    return {
        "model": config.CLAUDE_MODEL,
        "provider": config.AI_PROVIDER,
        "key_configured": bool(config.api_key()),
        "max_upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
        "allowed_types": list(config.ALLOWED_FORMATS.values()),
    }


@app.post("/api/generate")
def generate(file: UploadFile = File(...)):
    raw = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise AppError(400, "invalid_file", "빈 파일입니다. 이미지를 다시 선택해 주세요.")
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise AppError(413, "file_too_large", f"파일이 너무 큽니다. {config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB 이하로 올려 주세요.")
    photo = _prepare_photo(raw)

    if not _generate_lock.acquire(blocking=False):
        raise AppError(429, "busy", "이미 캐릭터를 만드는 중입니다. 끝날 때까지 기다려 주세요.")
    try:
        result = ai_client.generate(photo)
    except ai_client.AIError as e:
        log.warning("AI error %s: %s", e.code, e.detail)
        status = 500 if e.code == "config_error" else 502
        raise AppError(status, e.code, e.message, e.detail)
    except ai_client.AIFormatError as e:
        raise AppError(
            502, "ai_format_error",
            f"AI가 올바른 도트 데이터를 돌려주지 않았습니다 ({e.api_calls}회 시도). 다른 사진으로 다시 시도해 보세요.",
            "; ".join(e.errors[:5]),
        )
    finally:
        _generate_lock.release()

    sheet = build_sheet(result.grids["front"], result.grids["back"], result.grids["right"], result.palette)
    png = to_png_bytes(sheet)
    return {
        "sheet_png": "data:image/png;base64," + base64.standard_b64encode(png).decode("ascii"),
        "palette": result.palette,
        "frames": {k: ["".join(f"{v:X}" for v in row) for row in g] for k, g in result.grids.items()},
        "model": result.model,
        "api_calls": result.api_calls,
    }


@app.get("/reference/default_sheet.png")
def default_sheet_png():
    return FileResponse(config.REFERENCE_SHEET, media_type="image/png")


app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
