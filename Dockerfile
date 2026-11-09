FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check

COPY app/ app/
COPY static/ static/
COPY reference/ reference/

# Build-time check. It runs with no network and no API key, so it cannot reach
# the Anthropic API. A failure here fails the build, so nothing gets pushed.
RUN --network=none python - <<'PY'
import os
from pathlib import Path

for var in ("ANTHROPIC_API_KEY", "BAZE_API_KEY"):
    assert not os.getenv(var), f"{var} must not be set at build time"
assert not Path(".env").exists(), ".env must not be in the image"

required = [
    "reference/default_sheet.png",
    "static/index.html",
    "static/app.js",
    "static/style.css",
]
for name in required:
    assert Path(name).is_file(), f"missing file: {name}"

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

page = client.get("/")
assert page.status_code == 200 and "<html" in page.text.lower(), "main page did not load"
assert client.get("/reference/default_sheet.png").status_code == 200, "reference sheet not served"
assert client.get("/api/config").json()["key_configured"] is False, "key must be absent"

# Without a key, generation must stop with a clear error before any API call.
photo = Path("reference/default_sheet.png").read_bytes()
res = client.post("/api/generate", files={"file": ("photo.png", photo, "image/png")})
assert res.status_code == 500 and res.json()["error"] == "config_error", "no-key error not returned"

print("build check passed")
PY

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# ANTHROPIC_API_KEY is provided at run time only, e.g. docker run --env-file .env
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
