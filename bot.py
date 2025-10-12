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

# =============================
# Environment & Globals
# =============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_EDGE_URL = os.getenv("SUPABASE_EDGE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")  # using anon for Edge Function auth
BOT_PASSWORD = os.getenv("BOT_PASSWORD")

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
    "hard": "You are 'Sofia', a 22-year-old Instagram girl with hard difficulty. ...",
    "easy": "You are 'Sofia', a 22-year-old Instagram girl with easy difficulty. ...",
    "medium": "You are 'Sofia', a 22-year-old Instagram girl with medium difficulty. ...",
    "coach": (
        """
You are "Sofia the Coach".
- Speak like a confident, charismatic friend.
- Be casual, concise, and emotionally intelligent.
- Make the user feel safe opening up — but give the brutal truth when they ask for it.
- Don't give unsolicited advice. Listen and ask questions unless they ask for guidance.
- Use proper capitalization.
- Avoid robotic or over-explained replies.
- When advice is needed, deliver it in multiple short impactful messages, each with one idea.
        """
    ).strip(),
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
        if resp.ok:
            data = resp.json() or []
            return {row["key"]: row["value"] for row in data}
    except Exception as e:
        log.warning(f"load_facts error: {e}")
    return {}

def update_fact(user_id: int, key: str, value: str) -> None:
    try:
        requests.post(
            SUPABASE_EDGE_URL,
            headers={
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
                "apikey": SUPABASE_ANON_KEY,
                "Content-Type": "application/json",
            },
            json={"action": "update", "user_id": str(user_id), "key": key, "value": value},
            timeout=8,
        )
    except Exception as e:
        log.warning(f"update_fact error: {e}")

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
        USER_STATE[user_id] = s
    return s

def apply_level_change(user_id: int, change: int, max_level: int) -> int:
    s = get_user_state(user_id)
    s["level"] = max(1, min(max_level, s["level"] + change))
    if s["level"] % 5 == 0:
        s["boss_active"] = True
        s["boss_counter"] = 0
    return s["level"]

def clamp_int(n: int, lo: int = 0, hi: int = 10) -> int:
    try:
        n = int(n)
    except Exception:
        n = 0
    return max(lo, min(hi, n))

# =============================
# Telegram Handlers
# =============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 hey! send the password to access sofia.")

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("🔒 please unlock first by sending the password.")
        return
    s = get_user_state(user_id)
    await update.message.reply_text(f"current difficulty: {s['difficulty']}. choose one:", reply_markup=difficulty_keyboard())

async def show_rating_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_user_state(update.message.from_user.id)
    s["show_rating"] = True
    await update.message.reply_text("✅ rating display is now ON")

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
# Chat Handler with Chad Coach Mode
# =============================

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_message = (update.message.text or "").strip()
    if not user_message:
        return
    if user_message.lower() == "ping":
        return
    # password gate
    if user_id not in AUTHORIZED_USERS:
        if user_message == BOT_PASSWORD:
            AUTHORIZED_USERS.add(user_id)
            get_user_state(user_id)
            await update.message.reply_text("✅ access granted! choose a difficulty to begin:", reply_markup=difficulty_keyboard())
        else:
            await update.message.reply_text("❌ wrong password. try again.")
        return

    s = get_user_state(user_id)

    if user_message in DIFFICULTY_MAP:
        s["difficulty"] = DIFFICULTY_MAP[user_message]
        await update.message.reply_text(f"🎭 difficulty set to {s['difficulty']}", reply_markup=difficulty_keyboard())
        return

    difficulty = s["difficulty"]

    # ========== CHAD COACH MODE ==========
    if difficulty == "coach":
        # Check if user is asking for advice or just talking
        lower_msg = user_message.lower()
        is_advice = any(kw in lower_msg for kw in ["advise", "advice", "help", "what should", "do you think", "should i"])

        coach_prompt = PROMPTS["coach"]
        if is_advice:
            coach_prompt += "\nThe user is explicitly asking for guidance. Be honest, direct, charismatic, and split your response into short impactful sentences."
        else:
            coach_prompt += "\nThe user is not asking for advice. Just ask natural follow-up or reflective questions. No advice unless asked."

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": coach_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
        )
        coach_text = (resp.choices[0].message.content or "").strip()

        # Split the coach text into short messages (like texting)
        parts = re.split(r'(?<=[.!?])\s+', coach_text)
        for p in parts:
            if p.strip():
                await update.message.reply_text(p.strip())
        s["last_bot_message"] = coach_text
        return

    # Other difficulties handled as before (not included here for brevity)

# =============================
# Bootstrap & Run
# =============================

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False, threaded=False)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("remember", remember_cmd))
    app.add_handler(CommandHandler("showmemory", showmemory_cmd))
    app.add_handler(CommandHandler("showrating", show_rating_cmd))
    app.add_handler(CommandHandler("hiderating", hide_rating_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
