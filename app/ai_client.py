"""Ask Claude (Anthropic API directly, or the BAZE gateway) to draw the character as a palette + number grid.

Cost guard: at most 2 API calls per generate() — one attempt, plus one retry only when the
answer has a format problem. SDK-level automatic retries are disabled (max_retries=0).
"""
from __future__ import annotations

import base64
import io
import logging
import time
from dataclasses import dataclass

import anthropic
from PIL import Image

from . import config
from .sprite import CELL, Grid
from .validate import FormatError, extract_json, validate

log = logging.getLogger("sprite.ai")

MAX_CALLS = 2

SYSTEM_PROMPT = f"""You are a pixel artist who draws small game sprites as number grids.
You cannot output images, so you output JSON that encodes the pixels.

OUTPUT FORMAT (reply with this JSON object only: no markdown, no commentary):
{{
  "palette": ["transparent", "#RRGGBB", ...],
  "frames": {{
    "front": [ 32 strings ],
    "back":  [ 32 strings ],
    "right": [ 32 strings ]
  }}
}}
Rules:
- palette: at most 16 entries. Entry 0 is exactly "transparent". Every other entry is "#RRGGBB".
- Every frame is exactly {CELL} rows; every row is a string of exactly {CELL} characters.
- Each character is ONE hex digit (0-9, A-F) = the palette index of that pixel. 0 = transparent.
- Never use an index that is not in the palette.
- "front" faces the viewer (walking down the screen), "back" shows the back (walking up),
  "right" is a side view facing the viewer's right. Left is made by mirroring "right", so do not draw it.
- Keep rows 0-1 transparent and leave at least 1 transparent column on each side, so the
  character can bob. Feet/bottom should rest around row 29-30.
- All three views must be the same character with the same size, colors and proportions.

STYLE:
- Early-2000s handheld RPG overworld sprite: bold dark 1px outline around the whole silhouette,
  2-3 shading steps per color, cute big-head chibi proportions (head about half the height).
- Base the shape, colors and distinctive features (fur/hair color, markings, ears, clothes,
  accessories) on the subject in the user's photo.
- Draw an original character. Do not copy or imitate any existing game or anime character.
"""

USER_INSTRUCTIONS = """Image 1 is the FORMAT REFERENCE sprite sheet (a plain white blob placeholder, shown
enlarged 4x on a green background that is NOT part of the sprite). Rows top to bottom: facing down,
left, right, up; 4 walk frames per row; each cell is 32x32 pixels. Use it ONLY for layout, how each
direction looks (front: both eyes, side: one eye, back: no face), body proportion inside the 32x32
cell, and outline thickness. Do NOT copy its shape or colors.

Image 2 is the user's photo. Draw a new pixel-art character based on the animal or person in it,
keeping its colors and distinctive features.

Return only the JSON object with "palette" and the three frames "front", "back", "right"."""


@dataclass
class GenerationResult:
    palette: list[str]
    grids: dict[str, Grid]
    api_calls: int
    model: str


class AIError(Exception):
    """API/transport failure. `code` is a stable id for the frontend."""

    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class AIFormatError(Exception):
    def __init__(self, errors: list[str], api_calls: int):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors
        self.api_calls = api_calls


def _scrub(text: str) -> str:
    key = config.api_key()
    return text.replace(key, "<redacted>") if key else text


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


_reference_b64_cache: str | None = None


def reference_image_b64() -> str:
    """Default sheet enlarged 4x (nearest) on a flat background so the model can read the pixels."""
    global _reference_b64_cache
    if _reference_b64_cache is None:
        sheet = Image.open(config.REFERENCE_SHEET).convert("RGBA")
        bg = Image.new("RGBA", sheet.size, (120, 170, 110, 255))
        bg.alpha_composite(sheet)
        big = bg.convert("RGB").resize((sheet.width * 4, sheet.height * 4), Image.NEAREST)
        buf = io.BytesIO()
        big.save(buf, format="PNG")
        _reference_b64_cache = _b64(buf.getvalue())
    return _reference_b64_cache


def _client() -> anthropic.Anthropic:
    key = config.api_key()
    if not key:
        raise AIError("config_error", f"서버에 {config.KEY_VAR}가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return anthropic.Anthropic(
        api_key=key,
        base_url=config.BASE_URL,
        max_retries=0,  # never retry silently; every call is counted below
        timeout=config.REQUEST_TIMEOUT_S,
    )


def _call(client: anthropic.Anthropic, messages: list[dict]) -> tuple[str, str | None]:
    try:
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={"effort": config.CLAUDE_EFFORT},
        )
    except anthropic.APITimeoutError:
        raise AIError("api_timeout", "AI 응답 시간이 초과되었습니다. 잠시 후 다시 시도하세요.")
    except anthropic.APIConnectionError as e:
        raise AIError("api_connection", "AI 서버에 연결하지 못했습니다. 네트워크를 확인하세요.", _scrub(str(e)))
    except anthropic.APIStatusError as e:
        status = e.status_code
        detail = _scrub(str(getattr(e, "message", "") or e))[:500]
        friendly = {
            400: "AI 요청이 거절되었습니다 (요청 형식 또는 이미지 문제).",
            401: f"API 키 인증에 실패했습니다. .env의 {config.KEY_VAR}를 확인하세요.",
            403: "이 키로는 해당 모델을 사용할 권한이 없습니다.",
            404: "모델 이름이나 API 주소가 잘못되었습니다 (CLAUDE_MODEL / AI_PROVIDER 확인).",
            413: "업로드한 이미지가 API 허용 크기를 넘었습니다.",
            429: "요청 한도 또는 크레딧을 초과했습니다. 잠시 후 다시 시도하세요.",
        }.get(status, f"AI 서버 오류가 발생했습니다 (HTTP {status}).")
        raise AIError(f"api_http_{status}", friendly, detail)

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    usage = getattr(resp, "usage", None)
    log.info(
        "AI call ok: model=%s stop=%s blocks=%s in_tokens=%s out_tokens=%s chars=%d",
        getattr(resp, "model", "?"), resp.stop_reason, [b.type for b in resp.content],
        getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"), len(text),
    )
    return text, resp.stop_reason


def generate(photo_jpeg: bytes) -> GenerationResult:
    client = _client()
    messages: list[dict] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Image 1 (format reference sheet):"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": reference_image_b64()}},
                {"type": "text", "text": "Image 2 (user's photo):"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(photo_jpeg)}},
                {"type": "text", "text": USER_INSTRUCTIONS},
            ],
        }
    ]

    errors: list[str] = []
    for attempt in range(1, MAX_CALLS + 1):
        t0 = time.monotonic()
        text, stop = _call(client, messages)
        log.info("attempt %d took %.1fs", attempt, time.monotonic() - t0)
        try:
            if stop == "max_tokens":
                raise FormatError([f"answer was cut off at max_tokens={config.MAX_TOKENS}; output was incomplete"])
            if stop == "refusal":
                raise FormatError(["model refused to draw this image"])
            palette, grids = validate(extract_json(text))
            return GenerationResult(palette, grids, attempt, config.CLAUDE_MODEL)
        except FormatError as e:
            errors = e.errors
            log.warning("attempt %d format errors (%d): %s", attempt, len(errors), errors[:6])
            # A cut-off or refusal would just repeat under the same budget; only a complete
            # but malformed answer is worth the single corrective retry.
            if attempt == MAX_CALLS or stop in ("refusal", "max_tokens"):
                break
            # one corrective retry: show the model its answer and the exact problems
            messages = messages + [
                {"role": "assistant", "content": text or "(empty)"},
                {
                    "role": "user",
                    "content": "Your JSON did not pass validation:\n- "
                    + "\n- ".join(errors[:15])
                    + "\n\nReturn the corrected, complete JSON object only. Every frame must have exactly "
                    "32 rows of exactly 32 hex digits, and only use indices that exist in the palette.",
                },
            ]
    raise AIFormatError(errors, api_calls=attempt)
