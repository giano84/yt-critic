"""
YT Critic — Backend API su Railway.
Riceve il transcript già estratto dall'estensione Chrome.
Chiama Claude con web search per l'analisi critica.

Variabili d'ambiente Railway:
  ANTHROPIC_API_KEY  — da console.anthropic.com
  SECRET_KEY         — stringa casuale lunga
  FREE_TOTAL_LIMIT   — analisi gratis a vita per utente (default: 3)
  UPGRADE_URL        — link alla pagina di acquisto
"""

import os, re, hashlib, sqlite3
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SECRET_KEY        = os.environ.get("SECRET_KEY", "change-me")
FREE_TOTAL_LIMIT  = int(os.environ.get("FREE_TOTAL_LIMIT", "3"))
UPGRADE_URL       = os.environ.get("UPGRADE_URL", "https://paypal.me/giovannigrifa")
MODEL             = "claude-sonnet-4-6"
DB_PATH           = "/tmp/ytcritic.db"

# ── SQLite — usage persistente ────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage (token TEXT PRIMARY KEY, total INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    return conn

def user_key(token: str) -> str:
    return hashlib.sha256(f"{SECRET_KEY}:{token}".encode()).hexdigest()[:32]

def get_usage(token: str) -> int:
    key = user_key(token)
    with get_db() as conn:
        row = conn.execute("SELECT total FROM usage WHERE token = ?", (key,)).fetchone()
        return row[0] if row else 0

def increment_usage(token: str) -> int:
    key = user_key(token)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO usage (token, total) VALUES (?, 1) ON CONFLICT(token) DO UPDATE SET total = total + 1",
            (key,)
        )
        conn.commit()
        row = conn.execute("SELECT total FROM usage WHERE token = ?", (key,)).fetchone()
        return row[0]

# ── Claude con web search ─────────────────────────────────────────────────────

SYSTEM_PROMPT = """You must respond exclusively in English.

You are a critical analyst of video content. Read the transcript and produce
a structured critical summary in English with exactly this Markdown structure:

## Topic & Core Argument
(2-3 sentences: what the video is about and its main message)

## Key Points
(maximum 6-8 bullet points, no repetition or padding)

## Fact-Check
For each factual claim, statistic or verifiable data point in the video:
- Use the web_search tool to actually verify it before writing anything
- Classify as: ✅ Confirmed / ❌ False or misleading / ⚠️ Needs further verification
- Briefly cite the source used
If the video is purely opinion or entertainment, state that explicitly.

## Overall Critical Assessment
Is the video reliable? Any obvious bias? Opinions presented as facts?
Be direct and honest, even if the conclusion is negative.

Respond ONLY with the Markdown summary. Never invent content not in the transcript."""


def analyze_with_claude(title: str, transcript: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured on server")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{
            "role": "user",
            "content": f'Analyze this YouTube video titled "{title}".\n\nTRANSCRIPT:\n{transcript}'
        }],
        extra_headers={"anthropic-beta": "web-search-2025-03-05"},
    )

    text_parts = [
        block.text
        for block in response.content
        if hasattr(block, "text") and block.text
    ]
    result = "\n".join(text_parts).strip()
    if not result:
        raise RuntimeError("Claude returned an empty response — please retry")
    return result


# ── FastAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    get_db()
    if not ANTHROPIC_API_KEY:
        print("⚠️  WARNING: ANTHROPIC_API_KEY not set!")
    print(f"✅ YT Critic API ready — model: {MODEL} | free limit: {FREE_TOTAL_LIMIT}")
    yield


app = FastAPI(title="YT Critic API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: str
    user_token: str
    transcript: str
    title: str = "Video"


class AnalyzeResponse(BaseModel):
    title: str
    analysis: str
    used_total: int
    remaining: int
    free_limit: int


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.get("/limit")
def get_limit(user_token: str):
    used = get_usage(user_token)
    return {
        "used_total": used,
        "free_limit": FREE_TOTAL_LIMIT,
        "remaining": max(0, FREE_TOTAL_LIMIT - used),
        "upgrade_url": UPGRADE_URL,
    }


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    if not re.search(r"youtube\.com/watch|youtu\.be/", req.url):
        raise HTTPException(400, "Invalid URL: must be a YouTube link")

    if not req.transcript or len(req.transcript) < 100:
        raise HTTPException(422, "Transcript too short or missing")

    used = get_usage(req.user_token)
    if used >= FREE_TOTAL_LIMIT:
        raise HTTPException(
            402,
            f"You have used all {FREE_TOTAL_LIMIT} free analyses. Upgrade to continue: {UPGRADE_URL}"
        )

    try:
        analysis = analyze_with_claude(req.title, req.transcript)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(500, f"Internal error: {e}")

    new_total = increment_usage(req.user_token)
    return AnalyzeResponse(
        title=req.title,
        analysis=analysis,
        used_total=new_total,
        remaining=max(0, FREE_TOTAL_LIMIT - new_total),
        free_limit=FREE_TOTAL_LIMIT,
    )
