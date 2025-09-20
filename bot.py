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

# Remove when bot is public
BOT_PASSWORD = os.getenv("BOT_PASSWORD")
AUTHORIZED_USERS = set()


# Tiny Flask webserver for Render
app_flask = Flask(__name__)

@app_flask.route("/")
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
            await update.message.reply_text("✅ Access granted! You can now chat with me.")
        else:
            await update.message.reply_text("❌ Wrong password. Try again.")
        return

    # Ignore keep-alive pings
    if user_message.lower() == "ping":
        return

    # Send to OpenAI only if authorized
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]
    )
    reply = response.choices[0].message.content
    await update.message.reply_text(reply)


def keep_alive():
    url = "https://ai-chatbot-a8qt.onrender.com"
    params = {"chat_id": 6244527233, "text": "ping"}
    while True:
        try:
            requests.get(url, params=params)
            print("Keep-alive ping sent")
        except Exception as e:
            print("Keep-alive failed:", e)
        time.sleep(600)  # wait 10 minutes

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    threading.Thread(target=keep_alive, daemon=True).start()

    app.run_polling()

if __name__ == "__main__":
    # Run the Telegram bot in a background thread
    threading.Thread(target=main, daemon=True).start()

    # Run Flask so Render sees an open port
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)
