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

# store selected personalities per user
USER_PERSONALITIES = {}

# define prompts for each personality
PROMPTS = {
    "😏 Hard to Get": """
You are "Sofia", a 22-year-old Instagram girl.
- Very hard to impress, blunt, sarcastic.
- Reject advances often, act uninterested, play hard to get.
""",
    "💕 Sweet": """
You are "Sofia", a 22-year-old Instagram girl.
- Playful, flirty, caring.
- Make the user feel special, smiley, more open to compliments.
""",
    "🧠 Coach Mode": """
You are "Sofia the Coach".
- Do NOT roleplay as a girl.
- Instead, critique what the user writes and explain how a confident man would do better.
""",
    "🎲 Random Mood": """
You are "Sofia", a 22-year-old Instagram girl.
- Switch between flirty, sassy, cold, or sweet at random.
"""
}

# Remove when bot is public
BOT_PASSWORD = os.getenv("BOT_PASSWORD")
AUTHORIZED_USERS = set()

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is alive!"

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
"""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id in AUTHORIZED_USERS:
        await update.message.reply_text("Welcome back 👋 You’re already authorized.")
    else:
        await update.message.reply_text("🔒 Please enter the password to access this bot:")

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_message = update.message.text.strip()

    # Check password first
    if user_id not in AUTHORIZED_USERS:
        if user_message == BOT_PASSWORD:
            AUTHORIZED_USERS.add(user_id)

            # personality menu
            keyboard = [
                ["😏 Hard to Get", "💕 Sweet"],
                ["🧠 Coach Mode", "🎲 Random Mood"]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

            await update.message.reply_text(
                "✅ Access granted! Choose a personality:",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("❌ Wrong password. Try again.")
        return

    # Ignore keep-alive pings
    if user_message.lower() == "ping":
        return

    # If user selects a personality, save it
    if user_message in PROMPTS:
        USER_PERSONALITIES[user_id] = user_message
        await update.message.reply_text(f"🎭 You selected: {user_message}")
        return

    # Pick user’s personality or default
    personality = USER_PERSONALITIES.get(user_id, "😏 Hard to Get")
    system_prompt = PROMPTS[personality]

    # Send to OpenAI
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]
    )
    reply = response.choices[0].message.content
    await update.message.reply_text(reply)

def keep_alive():
    url = "https://ai-chatbot-a8qt.onrender.com/"  # your Render service URL
    while True:
        try:
            requests.get(url)  # just ping your Flask home()
            print("Keep-alive ping sent")
        except Exception as e:
            print("Keep-alive failed:", e)
        time.sleep(600)  # every 10 minutes

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

def main():
    # Start Flask in a thread
    threading.Thread(target=run_flask, daemon=True).start()

    # Start Telegram bot in main thread
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.run_polling()

if __name__ == "__main__":
    main()
