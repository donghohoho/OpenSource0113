"""Settings come from environment variables; the project-local .env is loaded into this process only."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)

DEFAULT_MODEL = "claude-opus-5-5"

# "anthropic": Anthropic API directly (ANTHROPIC_API_KEY). "baze": school gateway (BAZE_API_KEY).
PROVIDERS = {
    "anthropic": {
        "key_var": "ANTHROPIC_API_KEY",
        # explicit so a stray global ANTHROPIC_BASE_URL can't redirect requests
        "base_url": "https://api.anthropic.com",
    },
    "baze": {
        "key_var": "BAZE_API_KEY",
        "base_url": os.getenv("BAZE_BASE_URL") or "https://factchat-cloud.mindlogic.ai/v1/gateway/claude",
    },
}
AI_PROVIDER = (os.getenv("AI_PROVIDER") or "anthropic").strip().lower()
if AI_PROVIDER not in PROVIDERS:
    raise RuntimeError(f"AI_PROVIDER must be one of {sorted(PROVIDERS)}, got {AI_PROVIDER!r}")
KEY_VAR = PROVIDERS[AI_PROVIDER]["key_var"]
BASE_URL = PROVIDERS[AI_PROVIDER]["base_url"]

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL

# Commit SHA of the running build, set by `docker run -e APP_VERSION=...` at deploy time.
APP_VERSION = os.getenv("APP_VERSION") or "unknown"

# The JSON answer (3 frames x 32 rows x 32 hex chars) is ~2-3k tokens. Opus 5.5 always thinks and
# thinking tokens count toward max_tokens, so leave room for that at low effort.
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "16000"))
# Opus 5.5 cannot disable thinking; effort is the only knob (API default is "medium").
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "low")
REQUEST_TIMEOUT_S = 240

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

REFERENCE_SHEET = ROOT / "reference" / "default_sheet.png"
STATIC_DIR = ROOT / "static"


def api_key() -> str:
    """Read on demand so the value is never stored in a module-level name that could be dumped."""
    return os.getenv(KEY_VAR, "")
