"use strict";

// ---------------------------------------------------------------- constants
const CELL = 32;               // sprite cell size in the sheet
const SCALE = 3;               // on-screen zoom of the character
const ROWS = { down: 0, left: 1, right: 2, up: 3 };
const SPEED = 150;             // px per second (canvas pixels)
const WALK_FPS = 8;
const JUMP_VELOCITY = 420;     // px per second straight up at take-off
const GRAVITY = 1400;          // px per second^2  -> peak ~63px, ~0.6s in the air
const JUMP_PEAK = (JUMP_VELOCITY * JUMP_VELOCITY) / (2 * GRAVITY);
const DEFAULT_SHEET_URL = "/reference/default_sheet.png";
const ALLOWED_TYPES = ["image/jpeg", "image/png", "image/webp"];
let maxUploadBytes = 5 * 1024 * 1024;

const $ = (id) => document.getElementById(id);
const fileInput = $("file"), preview = $("preview"), dropText = $("drop-text"), drop = $("drop");
const genBtn = $("generate"), statusEl = $("status"), sheetImg = $("sheet"), sheetLabel = $("sheet-label");
const downloadLink = $("download"), resetBtn = $("reset");
const canvas = $("game"), ctx = canvas.getContext("2d");

// ---------------------------------------------------------------- status
function setStatus(text, kind = "", detail = "") {
  statusEl.className = "status " + kind;
  statusEl.textContent = text;
  if (detail) {
    const d = document.createElement("span");
    d.className = "detail";
    d.textContent = "자세한 정보: " + detail;
    statusEl.appendChild(d);
  }
}

// ---------------------------------------------------------------- server config (model name only)
fetch("/api/config").then((r) => r.json()).then((c) => {
  maxUploadBytes = c.max_upload_mb * 1024 * 1024;
  $("max-mb").textContent = c.max_upload_mb;
  $("model-info").textContent = `사용 모델: ${c.model} (${c.provider === "baze" ? "BAZE 게이트웨이" : "Anthropic API"})` + (c.key_configured ? "" : " — ⚠ 서버에 API 키가 설정되지 않았습니다");
}).catch(() => {});

// ---------------------------------------------------------------- upload + preview
let selectedFile = null;
let previewUrl = null;
let busy = false;

function pickFile(file) {
  selectedFile = null;
  genBtn.disabled = true;
  if (!file) return;
  if (!ALLOWED_TYPES.includes(file.type)) {
    setStatus(`지원하지 않는 파일 형식입니다 (${file.type || "알 수 없음"}). JPG, PNG, WEBP만 올릴 수 있어요.`, "error");
    return;
  }
  if (file.size > maxUploadBytes) {
    setStatus(`파일이 너무 큽니다 (${(file.size / 1048576).toFixed(1)}MB). ${maxUploadBytes / 1048576}MB 이하로 올려 주세요.`, "error");
    return;
  }
  selectedFile = file;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  preview.src = previewUrl;
  preview.hidden = false;
  dropText.hidden = true;
  genBtn.disabled = busy;
  setStatus("");
}

fileInput.addEventListener("change", () => pickFile(fileInput.files[0]));
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  pickFile(e.dataTransfer.files[0]);
});

// ---------------------------------------------------------------- generate (one request per click, no auto-retry)
genBtn.addEventListener("click", async () => {
  if (!selectedFile || busy) return;
  busy = true;
  genBtn.disabled = true;
  const started = Date.now();
  setStatus("캐릭터를 그리는 중… (보통 30초~2분)", "busy");
  const timer = setInterval(() => {
    setStatus(`캐릭터를 그리는 중… ${Math.round((Date.now() - started) / 1000)}초`, "busy");
  }, 1000);

  try {
    const form = new FormData();
    form.append("file", selectedFile);
    const res = await fetch("/api/generate", { method: "POST", body: form });
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON error page */ }
    if (!res.ok) {
      const msg = (data && data.message) || `서버 오류가 발생했습니다 (HTTP ${res.status}).`;
      setStatus(msg, "error", data && data.detail);
      return;
    }
    await useSheet(data.sheet_png, true);
    sheetLabel.textContent = `생성된 캐릭터 · ${data.model} · API 호출 ${data.api_calls}회`;
    setStatus(`완료! (${Math.round((Date.now() - started) / 1000)}초) 아래 게임 화면에서 움직여 보세요.`);
    canvas.focus();
  } catch (err) {
    setStatus("서버에 연결하지 못했습니다. 서버가 켜져 있는지 확인하세요.", "error", String(err));
  } finally {
    clearInterval(timer);
    busy = false;
    genBtn.disabled = !selectedFile;
  }
});

resetBtn.addEventListener("click", async () => {
  await useSheet(DEFAULT_SHEET_URL, false);
  sheetLabel.textContent = "기본 캐릭터 (줄: 아래 · 왼쪽 · 오른쪽 · 위 / 칸: 걷기 4프레임)";
});

// ---------------------------------------------------------------- sprite sheet handling
let sprite = null;                         // HTMLImageElement of the 128x128 sheet
let bbox = { x0: 0, y0: 0, x1: CELL, y1: CELL }; // opaque area inside a cell (for edge clamping)

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("이미지를 불러오지 못했습니다"));
    img.src = src;
  });
}

function opaqueBox(img) {
  const c = document.createElement("canvas");
  c.width = img.width; c.height = img.height;
  const g = c.getContext("2d");
  g.drawImage(img, 0, 0);
  const px = g.getImageData(0, 0, img.width, img.height).data;
  let x0 = CELL, y0 = CELL, x1 = 0, y1 = 0;
  for (let y = 0; y < img.height; y++) {
    for (let x = 0; x < img.width; x++) {
      if (px[(y * img.width + x) * 4 + 3] > 0) {
        const cx = x % CELL, cy = y % CELL;
        x0 = Math.min(x0, cx); y0 = Math.min(y0, cy);
        x1 = Math.max(x1, cx + 1); y1 = Math.max(y1, cy + 1);
      }
    }
  }
  return x1 > x0 ? { x0, y0, x1, y1 } : { x0: 0, y0: 0, x1: CELL, y1: CELL };
}

async function useSheet(src, generated) {
  const img = await loadImage(src);
  sprite = img;
  bbox = opaqueBox(img);
  sheetImg.src = src;
  downloadLink.href = src;
  downloadLink.download = generated ? "character_sprite_sheet.png" : "default_sprite_sheet.png";
  resetBtn.hidden = !generated;
  clampPlayer();
}

// ---------------------------------------------------------------- grass background (drawn once)
function makeGrass() {
  const T = 16, ZOOM = 4;                  // 16x16 pixel tile drawn at 4x = 64px
  const tile = document.createElement("canvas");
  tile.width = T; tile.height = T;
  const g = tile.getContext("2d");
  g.fillStyle = "#6fb05a"; g.fillRect(0, 0, T, T);
  let seed = 7;
  const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  for (let i = 0; i < 26; i++) {
    g.fillStyle = rand() < 0.5 ? "#62a04f" : "#7cbd66";
    g.fillRect(Math.floor(rand() * T), Math.floor(rand() * T), 1, 1);
  }
  for (let i = 0; i < 4; i++) {           // little grass blades
    const x = Math.floor(rand() * (T - 2)), y = Math.floor(rand() * (T - 3)) + 2;
    g.fillStyle = "#4f8c40";
    g.fillRect(x, y, 1, 2); g.fillRect(x + 2, y, 1, 2); g.fillRect(x + 1, y - 1, 1, 2);
  }
  const bg = document.createElement("canvas");
  bg.width = canvas.width; bg.height = canvas.height;
  const b = bg.getContext("2d");
  b.imageSmoothingEnabled = false;
  for (let y = 0; y < bg.height; y += T * ZOOM)
    for (let x = 0; x < bg.width; x += T * ZOOM)
      b.drawImage(tile, x, y, T * ZOOM, T * ZOOM);
  return bg;
}
const grass = makeGrass();

// ---------------------------------------------------------------- player + input
const player = { x: canvas.width / 2 - (CELL * SCALE) / 2, y: canvas.height / 2 - (CELL * SCALE) / 2, dir: "down", moving: false, anim: 0, z: 0, vz: 0 }; // z = height above the ground
const KEYMAP = {
  KeyW: "up", ArrowUp: "up", KeyS: "down", ArrowDown: "down",
  KeyA: "left", ArrowLeft: "left", KeyD: "right", ArrowRight: "right",
};
const held = new Set();

window.addEventListener("keydown", (e) => {
  if (e.code === "Space") {
    // leave Space alone on buttons/links/inputs so it can't re-click "캐릭터 만들기" or a download
    if (e.target instanceof HTMLElement && e.target.closest("button, a, input, textarea, select")) return;
    e.preventDefault();                    // stop Space from scrolling the page
    if (!e.repeat) jump();
    return;
  }
  const d = KEYMAP[e.code];
  if (!d) return;
  if (e.target instanceof HTMLInputElement) return;
  e.preventDefault();                      // stop arrow keys from scrolling the page
  held.add(d);
});
window.addEventListener("keyup", (e) => { const d = KEYMAP[e.code]; if (d) held.delete(d); });
window.addEventListener("blur", () => held.clear());
canvas.addEventListener("pointerdown", () => canvas.focus());

function clampPlayer() {
  // keep the visible (opaque) part of the sprite inside the canvas
  const minX = -bbox.x0 * SCALE, maxX = canvas.width - bbox.x1 * SCALE;
  const minY = -bbox.y0 * SCALE, maxY = canvas.height - bbox.y1 * SCALE;
  player.x = Math.min(Math.max(player.x, minX), maxX);
  player.y = Math.min(Math.max(player.y, minY), maxY);
}

function jump() {
  if (player.z === 0) player.vz = JUMP_VELOCITY;   // only from the ground: no double jump
}

function minSpriteY() {
  return -bbox.y0 * SCALE;
}

function update(dt) {
  let dx = 0, dy = 0;
  if (held.has("left")) dx -= 1;
  if (held.has("right")) dx += 1;
  if (held.has("up")) dy -= 1;
  if (held.has("down")) dy += 1;
  player.moving = dx !== 0 || dy !== 0;

  if (player.moving) {
    // facing: keep current direction while it is still part of the movement (diagonals)
    const wanted = [];
    if (dx < 0) wanted.push("left"); if (dx > 0) wanted.push("right");
    if (dy < 0) wanted.push("up"); if (dy > 0) wanted.push("down");
    if (!wanted.includes(player.dir)) player.dir = wanted[0];

    const len = Math.hypot(dx, dy);
    player.x += (dx / len) * SPEED * dt;
    player.y += (dy / len) * SPEED * dt;
    clampPlayer();
    player.anim += dt;
  } else {
    player.anim = 0;                       // stop on the standing frame, keep the last direction
  }

  if (player.z > 0 || player.vz > 0) {
    player.vz -= GRAVITY * dt;
    player.z += player.vz * dt;
    if (player.z <= 0) { player.z = 0; player.vz = 0; }   // landed
    // near the top edge the head would leave the canvas: stop rising there
    const headroom = player.y - minSpriteY();
    if (player.z > headroom) { player.z = Math.max(0, headroom); player.vz = Math.min(player.vz, 0); }
  }
}

function draw() {
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(grass, 0, 0);
  if (!sprite) return;
  const airborne = player.z > 0;
  // in the air hold frame 1 (body raised, feet tucked); on the ground walk or stand
  const frame = airborne ? 1 : player.moving ? Math.floor(player.anim * WALK_FPS) % 4 : 0;
  const row = ROWS[player.dir];
  const x = Math.round(player.x), y = Math.round(player.y);
  // shadow stays on the ground and shrinks/fades as the character rises
  const lift = Math.min(1, player.z / JUMP_PEAK);
  const k = 1 - 0.45 * lift;
  ctx.fillStyle = `rgba(0,0,0,${0.18 * (1 - 0.4 * lift)})`;
  ctx.beginPath();
  ctx.ellipse(x + ((bbox.x0 + bbox.x1) / 2) * SCALE, y + bbox.y1 * SCALE - 2,
              ((bbox.x1 - bbox.x0) / 2.6) * SCALE * k, 2.2 * SCALE * k, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.drawImage(sprite, frame * CELL, row * CELL, CELL, CELL, x, y - Math.round(player.z), CELL * SCALE, CELL * SCALE);
}

let last = performance.now();
function loop(now) {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  update(dt);
  draw();
  requestAnimationFrame(loop);
}

useSheet(DEFAULT_SHEET_URL, false)
  .catch(() => setStatus("기본 캐릭터 이미지를 불러오지 못했습니다.", "error"))
  .finally(() => requestAnimationFrame(loop));
