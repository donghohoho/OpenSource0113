# AI 도트 캐릭터 스프라이트 생성기

사진을 올리면 Claude(BAZE 게이트웨이 경유)가 32×32 도트 캐릭터를 숫자 격자(JSON)로 그리고,
서버가 걷기 애니메이션을 붙여 128×128 스프라이트 시트(4줄 × 4칸)로 만듭니다.

## 실행 (Windows PowerShell)

```powershell
cd <이 폴더>
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000 을 엽니다. 서버 종료는 `Ctrl+C`.
(가상환경을 activate하지 않고 python.exe를 직접 부르므로 실행 정책을 바꿀 필요가 없습니다.)

macOS/Linux: `.venv/bin/python -m uvicorn app.main:app --port 8000`

## 설정 (.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `AI_PROVIDER` | `anthropic` | `anthropic`(Anthropic API 직접) 또는 `baze`(학교 게이트웨이) |
| `ANTHROPIC_API_KEY` | (anthropic일 때 필수) | 서버에서만 읽고 브라우저로 보내지 않음 |
| `BAZE_API_KEY` | (baze일 때 필수) | 게이트웨이 키 |
| `BAZE_BASE_URL` | `https://factchat-cloud.mindlogic.ai/v1/gateway/claude` | baze일 때만 사용 |
| `CLAUDE_MODEL` | `claude-opus-5-5` | |
| `CLAUDE_EFFORT` | `low` | Opus 5.5는 thinking을 끌 수 없음. 생각량 조절 |
| `CLAUDE_MAX_TOKENS` | `16000` | thinking 토큰 포함 상한 |

## 구조

- `app/main.py` — FastAPI, 업로드 검사, `/api/generate`
- `app/ai_client.py` — 프롬프트, 호출(버튼 1회당 최대 2회, SDK 자동 재시도 끔)
- `app/validate.py` — 응답 JSON 엄격 검사
- `app/sprite.py` — 걷기 4프레임 생성, 시트 조립, 기본 캐릭터 그리기
- `scripts/make_default_sheet.py` — `reference/default_sheet.png` 재생성
- `static/` — 화면, Canvas 게임
