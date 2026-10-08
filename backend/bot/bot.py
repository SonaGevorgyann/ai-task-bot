import os
import json
import logging
from html import escape
from pathlib import Path

import httpx
import redis
import redis.asyncio as aioredis
from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL", "http://localhost:8000")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:3000").rstrip("/")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QUEUE = "transcription_jobs"
MAX_VOICE_SECONDS = 300

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

redis_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

HELP = (
    "Hi! I turn messages into tasks.\n\n"
    "• Send a text message to create a task\n"
    "• Send a voice message and I will transcribe it\n"
    "• /tasks shows your latest tasks\n"
    "• /board sends your private dashboard link\n\n"
    "The dashboard shows only your tasks. "
    "Move them between Pending, In Progress, and Completed, "
    "and I will message you when a status changes."
)

STATUS_LABELS = {
    "pending": "Pending",
    "in_progress": "In progress",
    "completed": "Completed",
}


def actor(update: Update):
    user = update.effective_user
    chat = update.effective_chat
    return {
        "telegram_chat_id": chat.id if chat else None,
        "telegram_id": user.id if user else None,
        "username": user.username if user else None,
        "first_name": user.first_name if user else None,
    }


async def api(method, path, **kwargs):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.request(method, f"{API_URL}{path}", **kwargs)
        response.raise_for_status()
        if response.content:
            return response.json()
        return None


def board_url(token):
    # Telegram's HTML parser drops links to "localhost" and shows the label as plain text.
    base = DASHBOARD_URL
    if base.startswith("http://localhost"):
        base = "http://127.0.0.1" + base[len("http://localhost"):]
    elif base.startswith("https://localhost"):
        base = "https://127.0.0.1" + base[len("https://localhost"):]
    return f"{base}/?board={token}"


def board_html(token):
    url = escape(board_url(token), quote=True)
    return f'Your private board: <a href="{url}">Open board</a>'


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def board_token_for(update: Update):
    user = update.effective_user
    if not user:
        return None
    data = await api(
        "POST",
        "/boards",
        json={
            "telegram_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
        },
    )
    return data["board_token"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = HELP
    parse_mode = None
    try:
        token = await board_token_for(update)
        if token:
            text += "\n\n" + board_html(token)
            parse_mode = "HTML"
    except httpx.HTTPError:
        log.error("Could not create a board link")
    await update.message.reply_text(text, parse_mode=parse_mode, link_preview_options=NO_PREVIEW)


async def board(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    try:
        token = await board_token_for(update)
    except httpx.HTTPError:
        log.error("Could not create a board link")
        await update.message.reply_text("⚠️ I couldn't make your board link. Please try again.")
        return
    if not token:
        return
    await update.message.reply_text(board_html(token), parse_mode="HTML", link_preview_options=NO_PREVIEW)


async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    try:
        token = await board_token_for(update)
        tasks = await api("GET", "/tasks", params={"board": token})
    except httpx.HTTPError:
        log.error("Could not load tasks")
        await update.message.reply_text("⚠️ I couldn't load your tasks. Please try again.")
        return

    tasks = (tasks or [])[:10]
    if not tasks:
        await update.message.reply_text("You have no tasks yet. Send me a text or voice message.")
        return

    lines = ["Your latest tasks:"]
    for task in tasks:
        label = STATUS_LABELS.get(task["status"], task["status"])
        text = task.get("text") or "Transcribing…"
        if task.get("transcription_status") == "processing":
            text = "Transcribing…"
        elif task.get("transcription_status") == "failed":
            text = "Transcription failed"
        if len(text) > 80:
            text = text[:77] + "…"
        lines.append(f"#{task.get('number') or task['id']} [{label}] {text}")
    await update.message.reply_text("\n".join(lines))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    payload = {"text": text, "source": "text", **actor(update)}
    try:
        task = await api("POST", "/tasks", json=payload)
        await update.message.reply_text(f"✅ Task #{task.get('number') or task['id']} created")
    except httpx.HTTPError:
        log.exception("Could not reach the API")
        await update.message.reply_text("⚠️ Sorry, I couldn't save your task. Please try again.")


async def mark_voice_failed(task_id):
    try:
        await api(
            "PATCH",
            f"/tasks/{task_id}",
            json={"transcription_status": "failed", "text": "[transcription failed]"},
        )
    except httpx.HTTPError:
        log.exception("Could not mark task %s as failed", task_id)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    voice = update.message.voice
    if voice.duration and voice.duration > MAX_VOICE_SECONDS:
        await update.message.reply_text("⚠️ That voice message is too long (max 5 minutes).")
        return

    status_msg = await update.message.reply_text("🎙 Transcribing…")
    payload = {
        "text": "",
        "source": "voice",
        "transcription_status": "processing",
        **actor(update),
    }
    task_id = None
    try:
        task = await api("POST", "/tasks", json=payload)
        task_id = task["id"]
        job = {
            "task_id": task_id,
            "number": task.get("number") or task_id,
            "file_id": voice.file_id,
            "chat_id": update.message.chat_id,
            "message_id": status_msg.message_id,
        }
        await redis_client.lpush(QUEUE, json.dumps(job))
    except (httpx.HTTPError, redis.RedisError):
        log.exception("Could not queue voice task")
        if task_id is not None:
            await mark_voice_failed(task_id)
        await status_msg.edit_text("⚠️ Sorry, I couldn't process your voice message. Please try again.")


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("board", board))
    app.add_handler(CommandHandler("tasks", my_tasks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.run_polling()


if __name__ == "__main__":
    main()
