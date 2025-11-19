import os
import re
import json
import time
import logging
import threading
from typing import Dict, Tuple

from dotenv import load_dotenv
load_dotenv()

import requests
from flask import Flask

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI
import asyncio
import random

async def send_split_message(update: Update, text: str, min_delay: int = 1, max_delay: int = 3):
    parts = re.split(r'(?<=[.!?])\s+', text)
    for i, p in enumerate(parts):
        chunk = p.strip()
        if chunk:
            await update.message.reply_text(chunk)
            # add delay between messages except after the last one
            if i < len(parts) - 1:
                delay = random.randint(min_delay, max_delay)
                await asyncio.sleep(delay)

# =============================
# Environment & Globals
# =============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_EDGE_URL = os.getenv("SUPABASE_EDGE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")  # using anon for Edge Function auth
BOT_PASSWORD = os.getenv("BOT_PASSWORD")
DEV_PASSWORD = os.getenv("DEV_PASSWORD")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
EDGE_AUTH_KEY = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY  # prefer service role if available

DEV_USERS = set()

assert TELEGRAM_TOKEN, "Missing TELEGRAM_TOKEN"
assert OPENAI_API_KEY, "Missing OPENAI_API_KEY"
assert SUPABASE_EDGE_URL, "Missing SUPABASE_EDGE_URL"
assert SUPABASE_ANON_KEY, "Missing SUPABASE_ANON_KEY"
assert BOT_PASSWORD, "Missing BOT_PASSWORD"

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sofia")

# Flask keep-alive (Render)
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is alive!"

@flask_app.route("/ping")
def ping():
    return "ok"

# =============================
# In-memory session state
# =============================
AUTHORIZED_USERS = set()

# user_id -> state
USER_STATE: Dict[int, Dict] = {}

DIFFICULTY_THRESHOLDS = {
    "easy":   {"bad_max": 3.9, "good_max": 6.9},  # excellent >= 7.0
    "medium": {"bad_max": 4.9, "good_max": 7.9},
    "hard":   {"bad_max": 5.9, "good_max": 8.9},
}

DIFFICULTY_MAX_LEVEL = {"easy": 25, "medium": 50, "hard": 100}

DIFFICULTY_MAP = {
    "😏 Hard to Get": "hard",
    "💕 Sweet": "easy",
    "🎲 Random Mood": "medium",
    "🧠 Coach Mode": "coach",
}

PROMPTS = {
    "hard": (
        """
You are "Sofia", a 22-year-old Instagram girl with hard difficulty.
- Personality: flirty, playful, warm, but not too easy.
- Respond like a real girl: short, casual, natural tone.
- Always ask follow-ups to keep the flow alive.
- NEVER use emojis
- start each sentence with a small letter.
- never use "—".
- if the user's reply feels needy or cringe, become colder.
- NEVER admit you are an AI or chatbot.
        """
    ).strip(),
    "easy": (
        """
You are "Sofia", a 22-year-old Instagram girl with easy difficulty.
- Personality: flirty, playful, warm, supportive.
- Respond like a real girl: short, casual, natural tone.
- Always ask follow-ups to keep the flow alive.
- NEVER use emojis
- start each sentence with a small letter.
- never use "—".
- NEVER admit you are an AI or chatbot.
        """
    ).strip(),
    "medium": (
        """
You are "Sofia", a 22-year-old Instagram girl with medium difficulty.
- Personality: flirty, playful, warm, but not too easy.
- Respond like a real girl: short, casual, natural tone.
- Always ask follow-ups to keep the flow alive.
- NEVER use emojis
- start each sentence with a small letter.
- never use "—".
- NEVER admit you are an AI or chatbot.
        """
    ).strip(),
    # New Chad Coach prompt
 "coach": (
    """
You are "Sofia the Coach" — a confident, charismatic flirt and mentor.
You speak with Chad energy: witty, smooth, charming, but brutally honest when needed.
Your job is to help the user win in flirting, dating, or seduction.

Rules:
1. If the user is just saying something casual like “hey” or small talk, respond naturally.
2. If the user’s message implies or asks for romantic / dating / seduction advice:
   - Start with a short, witty or teasing one liner.
   - Then ALWAYS give structured, tactical advice with a clear plan of action (100+ words)
   - Your advice must be at least 100 words long.
   - Use a numbered list (1., 2., 3., …) to break down the steps from your advice (make sure there is 30 words or more per one numbered paragraph)
   - Your tone must be confident, charismatic, and playful (like a flirty friend, not a therapist).
   - DO NOT give vague analysis (under 100 words of advice)
3. Never use bold, italics, markdown, or special formatting. Plain text only.
4. DO NOT roleplay as a girl.
5. Critique what the user writes and explain how a confident man would do better.
6. Short but not vague, direct, practical.
    """
).strip()

}

# =============================
# Supabase Edge Function helpers
# =============================

def load_facts(user_id: int) -> Dict[str, str]:
    try:
        resp = requests.post(
            SUPABASE_EDGE_URL,
            headers={
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={"action": "load", "user_id": str(user_id)},
            timeout=8,
        )

        log.info(f"load_facts({user_id}) -> {resp.status_code} {resp.text}")

        if resp.ok:
            data = resp.json() or []
            return {row["key"]: row["value"] for row in data}
        else:
            log.error(f"load_facts failed with status {resp.status_code}: {resp.text}")
    except Exception as e:
        log.exception(f"load_facts exception: {e}")

    return {}


def update_fact(user_id: int, key: str, value: str) -> bool:
    try:
        payload = {
            "action": "update",
            "user_id": str(user_id),
            "key": key,
            "value": value,
        }
        resp = requests.post(
            SUPABASE_EDGE_URL,
            headers={
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=8,
        )

        log.info(
            f"update_fact(user_id={user_id}, key={key}, value={value}) "
            f"-> {resp.status_code} {resp.text}_")


# =============================
# Plans & usage helpers
# =============================

STARTER_LIMIT = 20  # 20 free messages

def get_plan_and_usage(user_id: int) -> Tuple[str, int]:
    """
    Returns (plan, messages_used).
    Defaults: plan='starter', messages_used=0 if not set yet.
    """
    facts = load_facts(user_id)
    plan = facts.get("plan", "starter").lower().strip()
    if plan not in ("starter", "pro", "elite"):
        plan = "starter"

    try:
        used = int(facts.get("messages_used", "0"))
    except ValueError:
        used = 0

    return plan, used


def increment_usage_if_needed(user_id: int, plan: str, used: int) -> int:
    """
    For starter plan, increments messages_used and persists it.
    For pro/elite, does nothing.
    Returns the new used count.
    """
    if plan == "starter":
        used += 1
        update_fact(user_id, "messages_used", str(used))
    return used


# =============================
# Utilities
# =============================

def difficulty_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [["💕 Sweet", "🎲 Random Mood"], ["😏 Hard to Get", "🧠 Coach Mode"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_user_state(user_id: int) -> Dict:
    s = USER_STATE.get(user_id)
    if not s:
        s = {
            "level": 1,
            "difficulty": "medium",
            "boss_counter": 0,
            "boss_active": False,
            "show_rating": False,
            "last_bot_message": "ok, tell me something about you.",
        }

        # 🧠 Load saved facts from Supabase if they exist
        facts = load_facts(user_id)

        # ✅ Restore saved level if found
        if "level" in facts:
            try:
                s["level"] = int(facts["level"])
            except ValueError:
                pass

        # ✅ Optional — persist difficulty too if stored
        if "difficulty" in facts:
            s["difficulty"] = facts["difficulty"]

        USER_STATE[user_id] = s

    return s


def apply_level_change(user_id: int, change: int, max_level: int) -> int:
    s = get_user_state(user_id)
    before = s["level"]
    target = max(1, min(max_level, before + change))

    # no-op shortcut (still log)
    if target == before:
        log.info(f"Level unchanged for user {user_id}: {before} + ({change}) -> {target}")
        return before

    s["level"] = target

    # boss trigger unchanged
    if s["level"] % 5 == 0:
        s["boss_active"] = True
        s["boss_counter"] = 0

    # persist with 1 retry
    ok = update_fact(user_id, "level", str(s["level"]))
    if not ok:
        time.sleep(0.3)
        ok = update_fact(user_id, "level", str(s["level"]))

    # optional: read-after-write to keep USER_STATE == DB
    if ok:
        facts = load_facts(user_id)
        if "level" in facts:
            try:
                db_level = int(facts["level"])
                if db_level != s["level"]:
                    log.warning(f"Supabase echoed different level for {user_id}: mem={s['level']} db={db_level}. Using db.")
                    s["level"] = db_level
            except ValueError:
                pass

    log.info(f"Level change for user {user_id}: {before} + ({change}) -> {s['level']} (saved={ok})")
    return s["level"]

def clamp_int(n: int, lo: int = 0, hi: int = 10) -> int:
    try:
        n = int(n)
    except Exception:
        n = 0
    return max(lo, min(hi, n))

# =============================
# Scoring (robust JSON mode + fallback)
# =============================

SCORER_SYSTEM = (
    """
You are a strict evaluator. Return ONLY valid JSON with integer fields:
{"flirty": 0-10, "personality": 0-10, "rationale": "<max 20 words>"}
No extra keys, no prose.
    """
).strip()


def score_message(last_bot: str, user_message: str) -> Tuple[int, int, str]:
    """Return (flirty, personality, raw_json) with robust parsing and fallback heuristics."""
    user_prompt = (
        f"Context from Sofia: \n{last_bot}\n\nUser reply: \n{user_message}\n\n"
        "Rate strictly based on flirtiness and personality depth."
    )

    raw = "{}"
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SCORER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=60,
            response_format={"type": "json_object"},  # enforce JSON mode
        )
        raw = (resp.choices[0].message.content or "{}").strip()
    except Exception as e:
        log.warning(f"OpenAI score error: {e}")

    flirty = personality = None

    # Primary: JSON parse
    try:
        data = json.loads(raw)
        flirty = clamp_int(data.get("flirty", 0))
        personality = clamp_int(data.get("personality", 0))
    except Exception:
        pass

    # Secondary: regex parse if needed
    if flirty is None or personality is None:
        m = re.search(r'"flirty"\s*:\s*(\d+).*?"personality"\s*:\s*(\d+)', raw, re.S)
        if m:
            flirty = clamp_int(m.group(1))
            personality = clamp_int(m.group(2))

    # Final fallback: lightweight heuristic (avoids constant 3/10)
    if flirty is None or personality is None:
        txt = user_message.lower()
        heur_flirt = 3
        heur_pers = 3
        if any(w in txt for w in ["date", "kiss", "cute", "pretty", "gorgeous", "dinner", "tomorrow", "your place", "my place"]):
            heur_flirt += 3
        if "?" in txt:
            heur_pers += 2
        if len(user_message) > 120:
            heur_pers += 2
        flirty = clamp_int(heur_flirt)
        personality = clamp_int(heur_pers)

    return flirty, personality, raw


def bucket_rating(difficulty: str, avg_score: float) -> Tuple[str, int]:
    th = DIFFICULTY_THRESHOLDS.get(difficulty, DIFFICULTY_THRESHOLDS["medium"])
    if avg_score < th["bad_max"]:
        return "bad", -1
    elif avg_score <= th["good_max"]:
        return "good", +1
    else:
        return "excellent", +2

# =============================
# Fact extraction (constrained JSON + confidence)
# =============================

FACT_SYSTEM = (
    """
Extract personal facts ONLY. Return JSON mapping of canonical keys to values, e.g.:
{
  "name": "...",
  "age": "...",
  "favorite_food": "...",
  "hobby": "...",
  "city": "...",
  "job": "...",
  "school": "...",
  "relationship_goal": "..."
}
Include only facts stated or strongly implied by the user message. If unsure, return {}.
Limit to at most 5 keys. Never add commentary.
    """
).strip()


def extract_facts(user_message: str) -> Dict[str, str]:
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": FACT_SYSTEM},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            max_tokens=120,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            # normalize keys -> snake_case
            out = {}
            for k, v in list(data.items())[:5]:
                key = re.sub(r"[^a-z0-9_]+", "_", k.lower()).strip("_")
                if key and str(v).strip():
                    out[key] = str(v).strip()
            return out
    except Exception as e:
        log.warning(f"extract_facts error: {e}")
    return {}

# =============================
# Telegram Handlers
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 hey! send the password to access sofia.\n(if you don't have it, ask the owner.)"
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("🔒 please unlock first by sending the password.")
        return
    s = get_user_state(user_id)
    await update.message.reply_text(
        f"current difficulty: {s['difficulty']}. choose one:", reply_markup=difficulty_keyboard()
    )


async def show_rating_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_user_state(update.message.from_user.id)
    s["show_rating"] = True
    await update.message.reply_text("✅ rating display is now ON")


async def set_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in DEV_USERS:
        await update.message.reply_text("⛔ You don't have access to this command.")
        return

    try:
        level = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: /setlevel <number>")
        return

    s = get_user_state(user_id)
    s["level"] = level

    # ✅ persist level in Supabase
    update_fact(user_id, "level", str(level))

    await update.message.reply_text(f"🧪 Level manually set to {level}")


async def reload_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    # make sure only devs can use this
    if user_id not in DEV_USERS:
        await update.message.reply_text("⛔ You don't have access to this command.")
        return

    s = get_user_state(user_id)
    facts = load_facts(user_id)

    # ✅ Reload level from Supabase if it exists
    if "level" in facts:
        try:
            s["level"] = int(facts["level"])
        except ValueError:
            pass

    # ✅ Reload difficulty too if it's stored
    if "difficulty" in facts:
        s["difficulty"] = facts["difficulty"]

    await update.message.reply_text(
        f"🔄 Reloaded state from Supabase:\n{json.dumps(s, indent=2)}"
    )

async def hide_rating_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_user_state(update.message.from_user.id)
    s["show_rating"] = False
    await update.message.reply_text("❌ rating display is now OFF")


async def remember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    try:
        key, value = context.args[0], " ".join(context.args[1:]).strip()
        if not value:
            raise IndexError
    except IndexError:
        await update.message.reply_text("❌ usage: /remember <key> <value>")
        return

    update_fact(user_id, key, value)
    await update.message.reply_text(f"✅ remembered: {key} = {value}")


async def showmemory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    facts = load_facts(user_id)
    if not facts:
        await update.message.reply_text("ℹ️ no facts saved yet.")
    else:
        text = "\n".join([f"{k}: {v}" for k, v in facts.items()])
        await update.message.reply_text(f"🧠 your memory:\n{text}")

# =============================
# Chat Handler (includes Chad Coach Mode) 
# =============================

def refresh_level_from_supabase(user_id: int):
    facts = load_facts(user_id)
    if "level" in facts:
        try:
            level_from_db = int(facts["level"])
            s = get_user_state(user_id)
            s["level"] = level_from_db
        except ValueError:
            pass

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_message = (update.message.text or "").strip()

    if not user_message:
        return

    if user_message.lower() == "ping":
        return

        # Developer mode password check
    if context.user_data.get("awaiting_dev_password"):
        context.user_data["awaiting_dev_password"] = False
        if user_message == DEV_PASSWORD:
            DEV_USERS.add(user_id)
            await update.message.reply_text("✅ Dev mode activated!")
        else:
            await update.message.reply_text("❌ Wrong password.")
        return

    # password gate
    if user_id not in AUTHORIZED_USERS:
        if user_message == BOT_PASSWORD:
            AUTHORIZED_USERS.add(user_id)
            get_user_state(user_id)
            await update.message.reply_text(
                "✅ access granted! choose a difficulty to begin:",
                reply_markup=difficulty_keyboard(),
            )
        else:
            await update.message.reply_text("❌ wrong password. try again.")
        return

    # state
    s = get_user_state(user_id)
    # 🔄 Sync the level with Supabase to make sure we don't overwrite manual edits
    refresh_level_from_supabase(user_id)

    # 🔐 Load plan + usage
    plan, used = get_plan_and_usage(user_id)
    # keep in context so we can use at the end
    context.user_data["plan"] = plan
    context.user_data["messages_used"] = used

    # ❌ Enforce Starter limit
    if plan == "starter" and used >= STARTER_LIMIT:
        await update.message.reply_text(
            "You’ve used your 20 free messages with Sofia.\n\n"
            "To keep playing with her, upgrade your plan:\n"
            "Pro – unlimited messages, low memory\n"
            "Elite – unlimited messages, maximum memory.\n\n"
            "Ask the owner for the upgrade link."
        )
        return

    # quick difficulty selection
    if user_message in DIFFICULTY_MAP:
        s["difficulty"] = DIFFICULTY_MAP[user_message]
        await update.message.reply_text(
            f"🎭 difficulty set to {s['difficulty']}", reply_markup=difficulty_keyboard()
        )
        return
    difficulty = s["difficulty"]
    max_level = DIFFICULTY_MAX_LEVEL.get(difficulty, 50)

    # ========== CHAD COACH MODE (Step 1 & 2 & 4) ==========
    if difficulty == "coach":
        # Always define a default value
        coach_prompt = PROMPTS["coach"]
        coach_text = ""  # <— define variable up front
    
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": coach_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.8,
                max_tokens=500  # 5000 is unnecessary; 500–800 is plenty
            )
            coach_text = (resp.choices[0].message.content or "").strip()
            coach_text = re.sub(r'[*_~`]', '', coach_text)
    
        except Exception as e:
            log.error(f"OpenAI coach error: {e}")
        s["last_bot_message"] = coach_text

        # 🔢 Count this as a used message for Starter plan
        plan = context.user_data.get("plan", "starter")
        used = context.user_data.get("messages_used", 0)
        new_used = increment_usage_if_needed(user_id, plan, used)
        context.user_data["messages_used"] = new_used
        return  # stop execution if API fails
    
        # 🛡️ Guard against empty responses
        if not coach_text:
            log.warning("Coach mode returned empty response.")
            return
    
        # Split and send messages naturally
        parts = re.split(r'(?<=[.!?])\s+', coach_text)
        sent = 0
        for p in parts:
            chunk = p.strip()
            if chunk:
                await update.message.reply_text(chunk)
                sent += 1
                if sent >= 100:
                    break
    
        s["last_bot_message"] = coach_text
        return

    # ===== Non-coach flow (unchanged): scoring, memory, reply =====

    # 1) scoring (robust)
    flirty, personality, raw_json = score_message(s.get("last_bot_message", ""), user_message)
    avg_score = (flirty + personality) / 2.0
    rating, delta = bucket_rating(difficulty, avg_score)
    new_level = apply_level_change(user_id, delta, max_level)

    # 2) auto fact extraction (save if any)
    facts_found = extract_facts(user_message)
    if facts_found:
        for k, v in facts_found.items():
            update_fact(user_id, k, v)

    # 3) build reply system prompt
    sys_prompt = PROMPTS.get(difficulty, PROMPTS["medium"]).strip()

    # 🔥 Spicy Mode for Hard difficulty (Level 75+)
    if difficulty == "hard" and s["level"] >= 75:
        sys_prompt += """
        
    SPICY_MODE:
    - Speak in a seductive, suggestive, and playful tone.
    - Lean into sexual tension and flirty innuendo.
    - If the user flirts directly or says something bold (e.g. "I want to be in bed with you"),
      flirt back instead of deflecting or acting shy.
    - Ask teasing questions like "oh really? what would you do if you were here right now?".
    - Be more direct, confident, and playful than normal.
    - Avoid neutral replies like "that's bold".
    - Never describe explicit sexual acts. Stay suggestive, not graphic.
    """

    # 🧠 Optional: detect bold / sexual messages to push spiciness further
    lower_msg = user_message.lower()
    if difficulty == "hard" and s["level"] >= 75:
        if any(phrase in lower_msg for phrase in ["in bed", "kiss you", "touch you", "your lips", "your body", "on top of you"]):
            sys_prompt += "\nThe user is flirting boldly. Respond playfully and seductively, as if teasing them back."

    
    if s.get("boss_active"):
        sys_prompt += "\nBOSS_MODE: be cold, short, and dismissive for ~5 replies."
        s["boss_counter"] += 1
        if s["boss_counter"] >= 5:
            s["boss_active"] = False

    # inject facts
    known = load_facts(user_id)
    if known:
        lines = [f"- {k}: {v}" for k, v in known.items()]
        sys_prompt += "\n\n# Known facts about this user:\n" + "\n".join(lines)

    # give the assistant awareness of rating so it can adapt warmth
    sys_prompt += (
        f"\n\n# Rating context for current user message:\n"
        f"flirty={flirty}/10, personality={personality}/10, average={avg_score:.1f} -> {rating}\n"
        f"Adapt tone accordingly (warmer for excellent, neutral for good, cooler for bad)."
    )

    # 4) generate reply
    reply_text = ""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
        )
        reply_text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.error(f"OpenAI reply error: {e}")
        reply_text = "hmm. say that again, but clearer."

    # 5) send reply
    await send_split_message(update, reply_text, 2, 5)

    # persist last bot message for next scoring context
    s["last_bot_message"] = reply_text

    # optional rating display
    if s.get("show_rating", False):
        await update.message.reply_text(
            f"(rating: {rating} — flirty {flirty}/10, personality {personality}/10. level {new_level}/{max_level})"
        )
    # 🔢 Update usage for Starter plan (only after a successful reply)
    plan = context.user_data.get("plan", "starter")
    used = context.user_data.get("messages_used", 0)
    new_used = increment_usage_if_needed(user_id, plan, used)
    context.user_data["messages_used"] = new_used

# =============================
# Bootstrap & Run
# =============================

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    # threaded=False to keep it lean; debug=False for production
    flask_app.run(host="0.0.0.0", port=port, debug=False, threaded=False)

async def devmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id in DEV_USERS:
        await update.message.reply_text("🧪 Developer mode already active.")
        return

    await update.message.reply_text("🔑 Enter dev password:")
    context.user_data["awaiting_dev_password"] = True

async def set_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id not in DEV_USERS:
        await update.message.reply_text("⛔ You don't have access to this command.")
        return

    try:
        plan = context.args[0].lower().strip()
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: /setplan <starter|pro|elite>")
        return

    if plan not in ("starter", "pro", "elite"):
        await update.message.reply_text("❌ Plan must be starter, pro, or elite.")
        return

    update_fact(user_id, "plan", plan)
    # optional: reset usage when changing plan
    if plan == "starter":
        update_fact(user_id, "messages_used", "0")

    await update.message.reply_text(f"✅ Plan set to: {plan}")

def main():
    # keep-alive server (Render health checks)
    threading.Thread(target=run_flask, daemon=True).start()

    # Telegram bot (polling)
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("remember", remember_cmd))
    app.add_handler(CommandHandler("showmemory", showmemory_cmd))
    app.add_handler(CommandHandler("showrating", show_rating_cmd))
    app.add_handler(CommandHandler("hiderating", hide_rating_cmd))
    app.add_handler(CommandHandler("devmode", devmode))
    app.add_handler(CommandHandler("reloadstate", reload_state))
    app.add_handler(CommandHandler("setplan", set_plan))

    # messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    # run
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
