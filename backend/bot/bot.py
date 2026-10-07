import os
import logging
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL", "http://localhost:8000")

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a text message and I'll save it as a task."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = {
        "text": update.message.text,
        "source": "text",
        "telegram_chat_id": update.message.chat_id,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{API_URL}/tasks", json=payload)
            response.raise_for_status()
            task = response.json()
        await update.message.reply_text(f"✅ Task #{task['id']} created")
    except httpx.HTTPError:
        logging.exception("Could not reach the API")
        await update.message.reply_text("⚠️ Sorry, I couldn't save your task. Please try again.")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()


if __name__ == "__main__":
    main()