import os
import json
import logging
from pathlib import Path

import httpx
import redis
import redis.asyncio as aioredis
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL", "http://localhost:8000")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
QUEUE = "transcription_jobs"
MAX_VOICE_SECONDS = 300

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

redis_client = aioredis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me a text or voice message and I'll save it as a task."
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


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    if voice.duration > MAX_VOICE_SECONDS:
        await update.message.reply_text("⚠️ That voice message is too long (max 5 minutes).")
        return

    status_msg = await update.message.reply_text("🎙 Transcribing…")
    payload = {
        "text": "",
        "source": "voice",
        "telegram_chat_id": update.message.chat_id,
        "transcription_status": "processing",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{API_URL}/tasks", json=payload)
            response.raise_for_status()
            task = response.json()
        job = {
            "task_id": task["id"],
            "file_id": voice.file_id,
            "chat_id": update.message.chat_id,
            "message_id": status_msg.message_id,
        }
        await redis_client.lpush(QUEUE, json.dumps(job))
    except (httpx.HTTPError, redis.RedisError):
        logging.exception("Could not queue voice task")
        await status_msg.edit_text("⚠️ Sorry, I couldn't process your voice message. Please try again.")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.run_polling()


if __name__ == "__main__":
    main()