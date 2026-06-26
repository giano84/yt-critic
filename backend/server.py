"""
YT Critic — Backend API su Railway.
Riceve il transcript già estratto dall'estensione Chrome.
Chiama Claude con web search per l'analisi critica.

Variabili d'ambiente Railway:
  ANTHROPIC_API_KEY   — da console.anthropic.com
  SECRET_KEY          — stringa casuale lunga
  FREE_TOTAL_LIMIT    — analisi gratis a vita per utente (default: 3)
  GUMROAD_PRODUCT_ID  — product ID dal prodotto Gumroad (es. "abcde")
  UPGRADE_URL         — link alla pagina Gumroad di acquisto
"""

import os, re, hashlib, sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
SECRET_KEY           = os.environ.get("SECRET_KEY", "change-me")
FREE_TOTAL_LIMIT     = int(os.environ.get("FREE_TOTAL_LIMIT", "3"))
GUMROAD_PRODUCT_ID   = os.environ.get("GUMROAD_PRODUCT_ID", "")
UPGRADE_URL          = os.environ.get("UPGRADE_URL", "https://gumroad.com/l/ytcritic")
MODEL                = "claude-sonnet-4-6"
DB_PATH              = "/tmp/ytcritic.db"
CREDITS_PER_PURCHASE = 50

# ── SQLite ────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            token   TEXT PRIMARY KEY,
            used    INTEGER NOT NULL DEFAULT 0,
            credits INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS redeemed_keys (
            license_key TEXT PRIMARY KEY,
            token       TEXT NOT NULL,
            redeemed_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def migrate_db():
    with get_db() as conn:
        try:
            conn.execute("ALTER TABLE usage RENAME COLUMN total TO used")
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE usage ADD COLUMN credits INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            pass

def user_key(token: str) -> str:
    return hashlib.sha256(f"{SECRET_KEY}:{token}".encode()).hexdigest()[:32]

def get_usage(token: str) -> tuple[int, int]:
    key = user_key(token)
    with get_db() as conn:
        row = conn.execute("SELECT used, credits FROM usage WHERE token = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else (0, 0)

def increment_usage(token: str) -> tuple[int, int]:
    key = user_key(token)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO usage (token, used, credits) VALUES (?, 1, 0) "
            "ON CONFLICT(token) DO UPDATE SET used = used + 1",
            (key,)
        )
        conn.commit()
        row = conn.execute("SELECT used, credits FROM usage WHERE token = ?", (key,)).fetchone()
        return row[0], row[1]

def add_credits(token: str, amount: int) -> tuple[int, int]:
    key = user_key(token)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO usage (token, used, credits) VALUES (?, 0, ?) "
            "ON CONFLICT(token) DO UPDATE SET credits = credits + ?",
            (key, amount, amount)
        )
        conn.commit()
        row = conn.execute("SELECT used, credits FROM usage WHERE token = ?", (key,)).fetchone()
        return row[0], row[1]

# ── Gumroad license verification ──────────────────────────────────────────────

async def verify_gumroad_license(license_key: str) -> bool:
    if not GUMROAD_PRODUCT_ID:
        raise RuntimeError("GUMROAD_PRODUCT_ID not configured on server")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.gumroad.com/v2/licenses/verify",
            data={"product_id": GUMROAD_PRODUCT_ID, "license_key": license_key},
        )
        return resp.status_code == 200 and resp.json().get("success", False)

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
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{
            "role": "user",
            "content": f'Analyze this YouTube video titled "{title}".\n\nTRANSCRIPT:\n{transcript}'
        }],
        extra_headers={"anthropic-beta": "web-search-2025-03-05"},
    )

    if response.stop_reason == "max_tokens":
        print("⚠️  WARNING: Claude response was truncated (max_tokens reached)")

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
    migrate_db()
    get_db()
    if not ANTHROPIC_API_KEY:
        print("⚠️  WARNING: ANTHROPIC_API_KEY not set!")
    if not GUMROAD_PRODUCT_ID:
        print("⚠️  WARNING: GUMROAD_PRODUCT_ID not set — /redeem will fail")
    print(f"✅ YT Critic API ready — model: {MODEL} | free limit: {FREE_TOTAL_LIMIT} | credits per purchase: {CREDITS_PER_PURCHASE}")
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
    credits: int = 0
    total_limit: int = FREE_TOTAL_LIMIT


class RedeemRequest(BaseModel):
    user_token: str
    license_key: str


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.get("/limit")
def get_limit(user_token: str):
    used, credits = get_usage(user_token)
    total_limit = FREE_TOTAL_LIMIT + credits
    return {
        "used_total": used,
        "free_limit": FREE_TOTAL_LIMIT,
        "credits": credits,
        "total_limit": total_limit,
        "remaining": max(0, total_limit - used),
        "upgrade_url": UPGRADE_URL,
    }


@app.post("/redeem")
async def redeem(req: RedeemRequest):
    key = req.license_key.strip().upper()
    if not key:
        raise HTTPException(400, "License key is required")

    with get_db() as conn:
        existing = conn.execute(
            "SELECT token FROM redeemed_keys WHERE license_key = ?", (key,)
        ).fetchone()
        if existing:
            raise HTTPException(409, "This license key has already been activated")

    try:
        valid = await verify_gumroad_license(key)
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    if not valid:
        raise HTTPException(402, "Invalid license key. Please check your purchase confirmation email.")

    hashed_token = user_key(req.user_token)
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO redeemed_keys (license_key, token, redeemed_at) VALUES (?, ?, ?)",
                (key, hashed_token, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "This license key has already been activated")

    used, new_credits = add_credits(req.user_token, CREDITS_PER_PURCHASE)
    remaining = max(0, FREE_TOTAL_LIMIT + new_credits - used)

    return {
        "credits_added": CREDITS_PER_PURCHASE,
        "credits_total": new_credits,
        "remaining": remaining,
    }


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    if not re.search(r"youtube\.com/watch|youtu\.be/", req.url):
        raise HTTPException(400, "Invalid URL: must be a YouTube link")

    if not req.transcript or len(req.transcript) < 100:
        raise HTTPException(422, "Transcript too short or missing")

    used, credits = get_usage(req.user_token)
    total_allowed = FREE_TOTAL_LIMIT + credits
    if used >= total_allowed:
        raise HTTPException(
            402,
            f"You have used all {total_allowed} analyses. Purchase 50 more at: {UPGRADE_URL}"
        )

    try:
        analysis = analyze_with_claude(req.title, req.transcript)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(500, f"Internal error: {e}")

    new_used, credits = increment_usage(req.user_token)
    total_limit = FREE_TOTAL_LIMIT + credits
    return AnalyzeResponse(
        title=req.title,
        analysis=analysis,
        used_total=new_used,
        remaining=max(0, total_limit - new_used),
        free_limit=FREE_TOTAL_LIMIT,
        credits=credits,
        total_limit=total_limit,
    )
