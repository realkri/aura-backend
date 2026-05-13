from fastapi import FastAPI, File, UploadFile, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from typing import Optional
import os
import json
import uuid
import base64
import hashlib
import jwt
import time
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx
# from PIL import Image, ImageDraw, ImageFont  # Disabled — Render free tier
import io

# ============================================================
# OPTIONAL: MongoDB Atlas
# ============================================================
MONGO_URI = os.environ.get("MONGO_URI", "")
DB = None
if MONGO_URI:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        DB = mongo_client.get_database("aura")
    except Exception:
        DB = None

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="AURA", description="The AI Scorekeeper for Real Life")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

users_db = {}
verdicts_db = {}
battles_db = {}

JWT_SECRET = os.environ.get("JWT_SECRET", "aura-secret-change-me")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

gemini_last_call = 0
MIN_GEMINI_DELAY = 12

# ============================================================
# USER RANK SYSTEM — earned by lifetime Aura
# ============================================================
TIER_THRESHOLDS = [
    ("NPC", 0, "You exist. That's it."),
    ("BASE", 500, "Started trying. Barely."),
    ("MID", 1500, "Average. The photo equivalent of vanilla."),
    ("PRIME", 3500, "Solid. People might double-tap."),
    ("IMMORTAL", 7000, "Consistently clean. Built different."),
    ("MYTHIC", 12000, "Main character energy. The algorithm favors you."),
    ("AURAKAMI", 20000, "Legendary. Your fits are studied."),
    ("CELESTIAL", 35000, "Transcendent. Other people dress like YOU."),
    ("VOIDWALKER", 60000, "Reality bends around your drip."),
    ("OMEGA", 100000, "The final boss of fashion. Untouchable."),
]

def get_user_tier(lifetime_aura: int) -> dict:
    current = TIER_THRESHOLDS[0]
    next_tier = TIER_THRESHOLDS[1]
    for i, (name, threshold, desc) in enumerate(TIER_THRESHOLDS):
        if lifetime_aura >= threshold:
            current = (name, threshold, desc)
            next_tier = TIER_THRESHOLDS[i + 1] if i + 1 < len(TIER_THRESHOLDS) else None
        else:
            break
    progress = 0
    if next_tier:
        range_size = next_tier[1] - current[1]
        progress = min(100, max(0, int((lifetime_aura - current[1]) / range_size * 100)))
    else:
        progress = 100
    return {
        "tier": current[0],
        "tier_desc": current[2],
        "lifetime_aura": lifetime_aura,
        "next_tier": next_tier[0] if next_tier else None,
        "next_threshold": next_tier[1] if next_tier else None,
        "progress": progress
    }

# ============================================================
# DB HELPERS
# ============================================================
async def db_insert(collection: str, doc: dict):
    if DB:
        await DB[collection].insert_one(doc)
    else:
        if collection == "users":
            users_db[doc["id"]] = doc
        elif collection == "verdicts":
            verdicts_db[doc["id"]] = doc
        elif collection == "battles":
            battles_db[doc["id"]] = doc

async def db_find_one(collection: str, query: dict):
    if DB:
        return await DB[collection].find_one(query)
    else:
        if collection == "users":
            for u in users_db.values():
                if all(u.get(k) == v for k, v in query.items()):
                    return u
        elif collection == "verdicts":
            return verdicts_db.get(query.get("id"))
        elif collection == "battles":
            return battles_db.get(query.get("id"))
    return None

async def db_find(collection: str, query: dict = None, sort=None, limit=100):
    if DB:
        cursor = DB[collection].find(query or {})
        if sort:
            cursor = cursor.sort(sort[0], sort[1])
        return await cursor.to_list(length=limit)
    else:
        if collection == "users":
            items = list(users_db.values())
        elif collection == "verdicts":
            items = list(verdicts_db.values())
            if query and "user_id" in query:
                items = [v for v in items if v.get("user_id") == query["user_id"]]
        elif collection == "battles":
            items = list(battles_db.values())
        else:
            items = []
        if sort:
            items.sort(key=lambda x: x.get(sort[0], ""), reverse=sort[1] == -1)
        return items[:limit]

async def db_update_one(collection: str, query: dict, update: dict):
    if DB:
        await DB[collection].update_one(query, update)
    else:
        if collection == "users":
            for u in users_db.values():
                if all(u.get(k) == v for k, v in query.items()):
                    if "$inc" in update:
                        for k, v in update["$inc"].items():
                            u[k] = u.get(k, 0) + v
                    if "$set" in update:
                        u.update(update["$set"])
                    break

# ============================================================
# AUTH
# ============================================================
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=30),
        "type": "access"
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except:
        return None

async def get_current_user(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    payload = verify_token(token)
    if not payload:
        raise HTTPException(401, "Invalid token")
    user = await db_find_one("users", {"id": payload["sub"]})
    if not user:
        raise HTTPException(401, "User not found")
    return user

async def get_optional_user(request: Request):
    try:
        return await get_current_user(request)
    except:
        return None

# ============================================================
# AI JUDGE — UNFILTERED GEN Z VOICE
# ============================================================
# No corporate AI speak. Raw. Messy. Actually funny.

JUDGE_SYSTEM = """you are AURA. you dont capitalize. you dont use punctuation unless it slaps. you talk like youre in a groupchat roasting your friend at 2am.

rules:
- NEVER roast things people cant change (race body disability age). thats corny and we dont do corny.
- DO roast: fits that dont hit, poses that scream "i practiced this", backgrounds that look like a crime scene, lighting that makes them look like a ghost, energy that gives "my mom forced me to take this"
- if they ate, say they ate. if its mid, call it mid. if its bad, drag them with love.
- one-liners should be screenshot-worthy. the kind of thing someone posts to their story with "im crying"
- use slang naturally: "ate", "serving", "giving", "main character", "npc behavior", "rent free", "no notes", "the material", "understood the assignment", "its giving", "vibes are off", "drip check", "fit check"
- be specific. "the shirt" not "the clothing". "that pose" not "the composition". talk like you actually looked at the photo.
- sometimes be chaotic. sometimes be short. sometimes be unhinged. never be boring.
- if the photo is genuinely fire, hype them up like they just dropped a album. if its trash, be dramatic about it.

scoring:
- aura_score: 0-100 (main character energy confidence style "it factor")
- cringe_score: 0-100 (secondhand embarrassment tryhard awkward "my mom took this" vibes)
- most people are 30-60. you have to EARN high scores. dont hand out 80s like candy.

OUTPUT ONLY JSON no markdown no backticks no explanation:
{"aura_score":<int>,"cringe_score":<int>,"verdict_line":"<one savage sentence 6-12 words max>","reasoning":"<2 sentences be specific about what works and what doesnt>","tier":"<MID or PRIME or IMMORTAL or MYTHIC or AURAKAMI — this is PHOTO QUALITY not user rank>"}"""

BATTLE_SYSTEM = """you are AURA. groupchat energy. 2am roast session. two photos just dropped and youre picking a winner.

rules:
- no caps unless youre yelling
- specific roasts not generic
- winner gets hyped loser gets dragged
- screenshot worthy one-liners
- use slang naturally

OUTPUT ONLY JSON:
{"winner":"A"or"B","a_score":<int 0-100>,"b_score":<int 0-100>,"verdict_line":"<one sentence 8-14 words winner declared loser roasted>","reasoning":"<2 sentences why winner won why loser lost>"}"""

# ============================================================
# AI CALLERS
# ============================================================
async def call_gemini_with_retry(images_b64: list, prompt: str, system: str, max_retries: int = 3):
    global gemini_last_call
    elapsed = time.time() - gemini_last_call
    if elapsed < MIN_GEMINI_DELAY:
        await asyncio.sleep(MIN_GEMINI_DELAY - elapsed)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={GEMINI_API_KEY}"
    parts = [{"text": system + "\n\n" + prompt}]
    for img in images_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": img}})
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.95, "maxOutputTokens": 500}
    }

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=30)
                gemini_last_call = time.time()
                if response.status_code == 429:
                    wait = (2 ** attempt) + (hash(str(time.time())) % 1000 / 1000)
                    await asyncio.sleep(wait)
                    continue
                if response.status_code != 200:
                    raise Exception(f"Gemini HTTP {response.status_code}")
                data = response.json()
                if "candidates" not in data or not data["candidates"]:
                    raise Exception("No candidates")
                return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            await asyncio.sleep(2 ** attempt)
    raise Exception("Gemini retries exhausted")

async def call_openai_with_retry(images_b64: list, prompt: str, system: str, max_retries: int = 3):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    content = [{"type": "text", "text": prompt}]
    for img in images_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}})
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "max_tokens": 500,
        "temperature": 0.95
    }
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=headers, timeout=30)
                if response.status_code == 429:
                    wait = (2 ** attempt) + (hash(str(time.time())) % 1000 / 1000)
                    await asyncio.sleep(wait)
                    continue
                if response.status_code != 200:
                    raise Exception(f"OpenAI HTTP {response.status_code}")
                return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            await asyncio.sleep(2 ** attempt)
    raise Exception("OpenAI retries exhausted")

def parse_ai_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("json"):
        text = text[4:].strip()
    return json.loads(text)

# ============================================================
# JUDGE FUNCTIONS
# ============================================================
async def judge_image(image_b64: str, mode: str = "photo"):
    system = JUDGE_SYSTEM if mode == "photo" else BATTLE_SYSTEM
    prompt = "roast this photo. be specific. dont be boring." if mode == "photo" else "two photos. pick a winner. roast the loser."

    if GEMINI_API_KEY:
        try:
            raw = await call_gemini_with_retry([image_b64], prompt, system)
            return parse_ai_json(raw)
        except Exception as e:
            pass

    if OPENAI_API_KEY:
        try:
            raw = await call_openai_with_retry([image_b64], prompt, system)
            return parse_ai_json(raw)
        except Exception as e:
            pass

    return {
        "aura_score": 42,
        "cringe_score": 28,
        "verdict_line": "its giving i found this fit at the bottom of my closet",
        "reasoning": "the lighting is fighting for its life and that pose screams you practiced in the mirror for 20 minutes. not terrible not iconic.",
        "tier": "PRIME"
    }

async def judge_battle(image_a_b64: str, image_b_b64: str):
    prompt = "two photos. image 1 is A image 2 is B. pick a winner based on main character energy fit pose lighting vibe. be specific about why."

    if GEMINI_API_KEY:
        try:
            raw = await call_gemini_with_retry([image_a_b64, image_b_b64], prompt, BATTLE_SYSTEM)
            return parse_ai_json(raw)
        except Exception:
            pass

    if OPENAI_API_KEY:
        try:
            raw = await call_openai_with_retry([image_a_b64, image_b_b64], prompt, BATTLE_SYSTEM)
            return parse_ai_json(raw)
        except Exception:
            pass

    result_a = await judge_image(image_a_b64)
    result_b = await judge_image(image_b_b64)
    a_net = result_a.get("aura_score", 50) - result_a.get("cringe_score", 20)
    b_net = result_b.get("aura_score", 50) - result_b.get("cringe_score", 20)
    winner = "A" if a_net >= b_net else "B"
    return {
        "winner": winner,
        "a_score": result_a.get("aura_score", 50),
        "b_score": result_b.get("aura_score", 50),
        "verdict_line": f"side {winner} understood the assignment. side {'B' if winner == 'A' else 'A'} needs to go back to fashion school",
        "reasoning": f"side a had {result_a.get('aura_score', 50)} aura vs side b's {result_b.get('aura_score', 50)}. the gap was undeniable"
    }

# ============================================================
# MODELS
# ============================================================
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    handle: Optional[str] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

# ============================================================
# ROUTES
# ============================================================
@app.get("/api/")
async def root():
    return {"app": "AURA", "status": "alive", "version": "3.0", "db": "mongo" if DB else "memory"}

@app.post("/api/auth/register")
async def register(body: RegisterRequest):
    email = body.email.lower().strip()
    existing = await db_find_one("users", {"email": email})
    if existing:
        raise HTTPException(400, "Email already registered")
    user_id = str(uuid.uuid4())
    handle = (body.handle or email.split("@")[0]).strip()[:24] or "anon"
    doc = {
        "id": user_id,
        "email": email,
        "handle": handle,
        "password_hash": hash_password(body.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_plus": False,
        "lifetime_aura": 0,
        "lifetime_cringe": 0,
        "verdicts_count": 0
    }
    await db_insert("users", doc)
    token = create_token(user_id, email)
    tier_info = get_user_tier(0)
    return {**tier_info, "id": user_id, "email": email, "handle": handle, "is_plus": False, "token": token}

@app.post("/api/auth/login")
async def login(body: LoginRequest):
    email = body.email.lower().strip()
    user = await db_find_one("users", {"email": email})
    if not user or user["password_hash"] != hash_password(body.password):
        raise HTTPException(401, "Invalid credentials")
    token = create_token(user["id"], email)
    tier_info = get_user_tier(user.get("lifetime_aura", 0))
    return {**tier_info, "id": user["id"], "email": email, "handle": user["handle"], "is_plus": user.get("is_plus", False), "token": token}

@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    tier_info = get_user_tier(user.get("lifetime_aura", 0))
    clean = {k: v for k, v in user.items() if k != "password_hash"}
    return {**clean, **tier_info}

# ============================================================
# PHOTO JUDGE
# ============================================================
@app.post("/api/judge/photo")
async def judge_photo(request: Request, photo: UploadFile = File(...)):
    user = await get_optional_user(request)
    if photo.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(400, "Unsupported file type")
    contents = await photo.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large")

    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[photo.content_type]
    filename = f"{uuid.uuid4().hex}.{ext}"
    with open(UPLOAD_DIR / filename, "wb") as f:
        f.write(contents)

    image_b64 = base64.b64encode(contents).decode()
    verdict = await judge_image(image_b64)

    aura_score = max(0, min(100, verdict.get("aura_score", 50)))
    cringe_score = max(0, min(100, verdict.get("cringe_score", 20)))

    vid = str(uuid.uuid4())
    doc = {
        "id": vid,
        "mode": "photo",
        "image_name": filename,
        "image_url": f"/api/uploads/{filename}",
        "aura_score": aura_score,
        "cringe_score": cringe_score,
        "net_score": aura_score - cringe_score,
        "verdict_line": verdict.get("verdict_line", "mid energy")[:200],
        "reasoning": verdict.get("reasoning", "")[:1000],
        "photo_tier": verdict.get("tier", "PRIME"),
        "user_id": user["id"] if user else None,
        "user_handle": user["handle"] if user else "anon",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db_insert("verdicts", doc)

    if user:
        await db_update_one("users", {"id": user["id"]}, {
            "$inc": {"lifetime_aura": aura_score, "lifetime_cringe": cringe_score, "verdicts_count": 1}
        })

    # Return with updated tier info if user is logged in
    result = dict(doc)
    if user:
        updated_user = await db_find_one("users", {"id": user["id"]})
        if updated_user:
            tier_info = get_user_tier(updated_user.get("lifetime_aura", 0))
            result["user_ti
