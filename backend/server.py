"""
YT Critic — Backend API su Railway.
Riceve il transcript già estratto dall'estensione Chrome.
Chiama Claude con web search per l'analisi critica.

Variabili d'ambiente Railway:
  ANTHROPIC_API_KEY  — da console.anthropic.com
  SECRET_KEY         — stringa casuale lunga
  FREE_DAILY_LIMIT   — analisi gratis al giorno (default: 1)
"""

import os, re, hashlib, datetime
from collections import defaultdict
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SECRET_KEY        = os.environ.get("SECRET_KEY", "change-me")
FREE_DAILY_LIMIT  = int(os.environ.get("FREE_DAILY_LIMIT", "1"))
MODEL             = "claude-sonnet-4-6"

# ── Rate limiting in-memory ───────────────────────────────────────────────────

usage: dict[str, int] = defaultdict(int)

def rate_key(token: str) -> str:
    today = datetime.date.today().isoformat()
    h = hashlib.sha256(f"{SECRET_KEY}:{token}".encode()).hexdigest()[:16]
    return f"{today}:{h}"

def check_and_increment(token: str) -> tuple[bool, int]:
    key = rate_key(token)
    n = usage[key]
    if n >= FREE_DAILY_LIMIT:
        return False, 0
    usage[key] = n + 1
    return True, FREE_DAILY_LIMIT - (n + 1)

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

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2048,
        betas=["web-search-2025-03-05"],
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{
            "role": "user",
            "content": f'Analyze this YouTube video titled "{title}".\n\nTRANSCRIPT:\n{transcript}'
        }],
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
    if not ANTHROPIC_API_KEY:
        print("⚠️  WARNING: ANTHROPIC_API_KEY not set!")
    print(f"✅ YT Critic API ready — model: {MODEL}")
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
    transcript: str   # estratto dal browser dall'estensione Chrome
    title: str = "Video"


class AnalyzeResponse(BaseModel):
    title: str
    analysis: str
    remaining_today: int


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.get("/limit")
def get_limit(user_token: str):
    key = rate_key(user_token)
    used = usage[key]
    return {
        "used_today": used,
        "limit": FREE_DAILY_LIMIT,
        "remaining": max(0, FREE_DAILY_LIMIT - used),
    }


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    if not re.search(r"youtube\.com/watch|youtu\.be/", req.url):
        raise HTTPException(400, "Invalid URL: must be a YouTube link")

    if not req.transcript or len(req.transcript) < 100:
        raise HTTPException(422, "Transcript too short or missing")

    allowed, remaining = check_and_increment(req.user_token)
    if not allowed:
        raise HTTPException(
            429,
            f"Daily limit of {FREE_DAILY_LIMIT} free analyses reached. Come back tomorrow."
        )

    try:
        analysis = analyze_with_claude(req.title, req.transcript)
        return AnalyzeResponse(
            title=req.title,
            analysis=analysis,
            remaining_today=remaining,
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        raise HTTPException(500, f"Internal error: {e}")
