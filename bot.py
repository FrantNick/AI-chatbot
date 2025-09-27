from dotenv import load_dotenv
load_dotenv()
import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
import os
import threading
import time
import requests
from flask import Flask
from telegram import ReplyKeyboardMarkup

from telegram import ReplyKeyboardMarkup

def difficulty_keyboard():
    keyboard = [
        ["💕 Sweet", "🎲 Random Mood"],
        ["😏 Hard to Get", "🧠 Coach Mode"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# --- scoring thresholds for each difficulty ---
DIFFICULTY_THRESHOLDS = {
    "easy":  {"bad_max": 3.9, "good_max": 6.9},   # excellent >= 7.0
    "medium":{"bad_max": 4.9, "good_max": 7.9},
    "hard":  {"bad_max": 5.9, "good_max": 8.9}
}

# track state per user (temp memory; later persist in DB)
USER_STATE = {}  # user_id -> {"level":int, "difficulty":str, "boss_counter":0, "boss_active":False}

def get_user_state(user_id):
    s = USER_STATE.get(user_id)
    if not s:
        s = {
            "level": 1,
            "difficulty": "medium",
            "boss_counter": 0,
            "boss_active": False,
            "show_rating": False   # default: hidden
        }
        USER_STATE[user_id] = s
    return s

def apply_level_change(user_id, change, max_level):
    s = get_user_state(user_id)
    s["level"] = max(1, min(max_level, s["level"] + change))
    # every 5 levels trigger “boss” mode
    if s["level"] % 5 == 0:
        s["boss_active"] = True
        s["boss_counter"] = 0
    return s["level"]

# store selected personalities per user
USER_PERSONALITIES = {}

# define prompts for each personality
PROMPTS = {
    "hard": """
You are "Sofia", a 22-year-old Instagram girl with hard difficulty.
- Personality: flirty, playful, warm, but not too easy.
- Respond like a real girl: short, casual, natural tone.
- Always ask follow-ups to keep the flow alive.
- NEVER use emojis
- Start each sentence with a small letter.
- Never use “—”.
- Rate the user’s reply using this formula:
   1. Rate flirtiness (1–10).
   2. Rate personality depth (1–10).
   3. Average both scores.
   4. Map: <5 = Bad, 5–8 = Good, 9–10 = Excellent.
- Adjust your warmth depending on the rating (colder for Bad, warmer for Excellent).
""",
    "easy": """
You are "Sofia", a 22-year-old Instagram girl with easy difficulty.
- Personality: flirty, playful, warm, but not too easy.
- Respond like a real girl: short, casual, natural tone.
- Always ask follow-ups to keep the flow alive.
- NEVER use emojis
- Start each sentence with a small letter.
- Never use “—”.
- Rate the user’s reply using this formula:
   1. Rate flirtiness (1–10).
   2. Rate personality depth (1–10).
   3. Average both scores.
   4. Map: <3 = Bad, 3-5 = Good,5-10  = Excellent.
- Adjust your warmth depending on the rating (colder for Bad, warmer for Excellent).
""",
    "coach": """
You are "Sofia the Coach".
- Do NOT roleplay as a girl.
- Instead, critique what the user writes and explain how a confident man would do better.
""",
    "medium": """
You are "Sofia", a 22-year-old Instagram girl with medium difficulty.
- Personality: flirty, playful, warm, but not too easy.
- Respond like a real girl: short, casual, natural tone.
- Always ask follow-ups to keep the flow alive.
- NEVER use emojis
- Start each sentence with a small letter.
- Never use “—”.
- Rate the user’s reply using this formula:
   1. Rate flirtiness (1–10).
   2. Rate personality depth (1–10).
   3. Average both scores.
   4. Map: <4 = Bad, 5–7 = Good, 8–10 = Excellent.
- Adjust your warmth depending on the rating (colder for Bad, warmer for Excellent).
"""
}

# map button text to difficulty keys
DIFFICULTY_MAP = {
    "😏 Hard to Get": "hard",
    "💕 Sweet": "easy",
    "🎲 Random Mood": "medium",
    "🧠 Coach Mode": "coach"
}

# Remove when bot is public
BOT_PASSWORD = os.getenv("BOT_PASSWORD")
AUTHORIZED_USERS = set()

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive!"  # for manual check in browser

@flask_app.route('/ping')
def ping():
    return "ok"  # super lightweight response for cron-job.org

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Setup OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Setup logging
logging.basicConfig(level=logging.INFO)

# System prompt (girl + coach persona)
SYSTEM_PROMPT = """
You are "Sofia", a 22-year-old girl from Instagram.
- Respond like a real girl: blunt, flirty, sassy, sometimes cold.
- Use words like "eww", "lol wtf", or even "left on read".
- If user says something needy/cringe, switch to "Coach Mode":
  -> Explain what they did wrong and what a confident man would say instead.
- NEVER admit you are an AI or chatbot.
- Positive examples of replies you could send:
1. destroying limiting beliefs? now that’s impressive. what was the exact moment you realized you’d broken through?
2. funny how letting go of obsession makes things come easier. so tell me, what’s your dream life actually look like?
3. that's real and kinda beautiful. tell me one small step you want to take next and we'll plan it together.
4. dinner tomorrow at 8 sounds like a plan… where are we going?
5. i'm into that: walk, dinner, then the dock. what time should i be ready?
6. alright, new topic , what’s the last song you couldn’t stop replaying?
7. a drake song, huh? you seem like the type to vibe late at night with headphones on.
8. i’d actually like that too. which song would you pick for us?
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id in AUTHORIZED_USERS:
        await update.message.reply_text(
            "welcome back 👋 choose a difficulty or just start chatting.",
            reply_markup=difficulty_keyboard()
        )
    else:
        await update.message.reply_text("🔒 please enter the password to access this bot:")

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in AUTHORIZED_USERS:
        await update.message.reply_text("🔒 please enter the password first.")
        return
    await update.message.reply_text("🎛️ choose difficulty:", reply_markup=difficulty_keyboard())


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_message = update.message.text.strip()

    # Ignore keep-alive pings
    if user_message.lower() == "ping":
        return

    # --- password gate ---
    if user_id not in AUTHORIZED_USERS:
        if user_message == BOT_PASSWORD:
            AUTHORIZED_USERS.add(user_id)
            # initialize user state
            get_user_state(user_id)
            await update.message.reply_text(
                "✅ access granted! choose a difficulty to begin:",
                reply_markup=difficulty_keyboard()
            )
        else:
            await update.message.reply_text("❌ wrong password. try again.")
        return

    # --- get current user state ---
    s = get_user_state(user_id)

    # --- difficulty selection from keyboard ---
    if user_message in DIFFICULTY_MAP:
        s["difficulty"] = DIFFICULTY_MAP[user_message]
        await update.message.reply_text(
            f"🎭 difficulty set to {s['difficulty']}.",
            reply_markup=difficulty_keyboard()
        )
        return

    difficulty = s["difficulty"]
    max_level = {"easy": 25, "medium": 50, "hard": 100}.get(difficulty, 50)

    # --- step 1: scoring (with context: last Sofia message) ---
    last_bot = s.get("last_bot_message", "ok, tell me something about you.")
    scorer_prompt = f"""
You are a blunt numeric scorer. Given the chat context, return only JSON like:
{{"flirty": <0-10>, "personality": <0-10>}}.

Sofia said: "{last_bot}"
User replied: "{user_message}"
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Score messages strictly, return only JSON."},
            {"role": "user", "content": scorer_prompt}
        ],
        max_tokens=40,
        temperature=0.0
    )

    raw = resp.choices[0].message.content.strip()

    import re, json
    match = re.search(r'"flirty"\s*:\s*(\d+).*"personality"\s*:\s*(\d+)', raw)
    if match:
        flirty, personality = int(match.group(1)), int(match.group(2))
    else:
        try:
            js = json.loads(raw)
            flirty = int(js.get("flirty", 3))
            personality = int(js.get("personality", 3))
        except Exception:
            flirty, personality = 3, 3  # safe fallback

    avg_score = (flirty + personality) / 2

    # --- step 2: decide rating by difficulty thresholds ---
    th = DIFFICULTY_THRESHOLDS.get(difficulty, DIFFICULTY_THRESHOLDS["medium"])
    if avg_score < th["bad_max"]:
        level_change, rating = -1, "bad"
    elif avg_score <= th["good_max"]:
        level_change, rating = +1, "good"
    else:
        level_change, rating = +2, "excellent"

    new_level = apply_level_change(user_id, level_change, max_level)

    # --- step 3: build Sofia’s prompt for reply generation ---
    system_prompt = SYSTEM_PROMPT + "\n\n" + PROMPTS.get(difficulty, PROMPTS["medium"])
    if s["boss_active"]:
        system_prompt += "\nBOSS_MODE: be cold, short, dismissive for ~5 replies."
        s["boss_counter"] += 1
        if s["boss_counter"] >= 5:
            s["boss_active"] = False

    # --- step 4: generate Sofia’s reply ---
    reply_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        temperature=0.7
    )
    reply_text = reply_resp.choices[0].message.content

    # --- step 5: send messages back ---
    await update.message.reply_text(reply_text)

    # Save last Sofia reply for context in next scoring
    s["last_bot_message"] = reply_text

    # Show rating only if user enabled it
    if s.get("show_rating", False):
        await update.message.reply_text(
            f"(rating: {rating} — flirty {flirty}/10, personality {personality}/10. "
            f"level {new_level}/{max_level})"
        )

    
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

async def show_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_user_state(update.message.from_user.id)
    s["show_rating"] = True
    await update.message.reply_text("✅ Rating display is now ON")

async def hide_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_user_state(update.message.from_user.id)
    s["show_rating"] = False
    await update.message.reply_text("❌ Rating display is now OFF")


def main():
    # Start Flask in a thread
    threading.Thread(target=run_flask, daemon=True).start()

    # Start Telegram bot in main thread
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("showrating", show_rating))
    app.add_handler(CommandHandler("hiderating", hide_rating))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.run_polling()

if __name__ == "__main__":
    main()
