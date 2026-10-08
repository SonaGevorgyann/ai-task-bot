import os
import json
import time
import logging
from pathlib import Path

import httpx
import redis
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from app.database import SessionLocal
from app.models import Task
from app.events import NOTIFY_QUEUE, publish_event

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QUEUE = "transcription_jobs"
MAX_ATTEMPTS = 3

STT_MODEL = os.getenv("STT_MODEL", "whisper-1")

STATUS_LABELS = {
    "pending": "Pending",
    "in_progress": "In Progress",
    "completed": "Completed ✅",
}

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("worker")

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
stt_client = OpenAI(
    api_key=os.getenv("STT_API_KEY"),
    base_url=os.getenv("STT_BASE_URL", "https://api.openai.com/v1"),
)


class EmptyTranscription(Exception):
    pass


def safe(error):
    """Error text with the bot token masked, so it never reaches the logs."""
    return str(error).replace(TOKEN or "", "***")


def tg(method, **payload):
    resp = httpx.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()["result"]


def edit_message(chat_id, message_id, text):
    try:
        tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
    except Exception as e:
        log.warning("Could not edit Telegram message: %s", safe(e))


def update_task(task_id, **fields):
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task:
            for key, value in fields.items():
                setattr(task, key, value)
            db.commit()
            db.refresh(task)
            publish_event("task_updated", task)


def download_voice(file_id):
    info = tg("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}"
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def transcribe(audio):
    result = stt_client.audio.transcriptions.create(
        model=STT_MODEL, file=("voice.ogg", audio, "audio/ogg")
    )
    return result.text.strip()


def with_retries(fn, *args):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn(*args)
        except Exception as e:
            log.warning("%s failed (attempt %s/%s): %s", fn.__name__, attempt, MAX_ATTEMPTS, safe(e))
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(2 ** attempt)


def fail(job, message):
    update_task(job["task_id"], transcription_status="failed", text="[transcription failed]")
    edit_message(job["chat_id"], job["message_id"], f"⚠️ {message}")


def handle_job(job):
    audio = with_retries(download_voice, job["file_id"])
    text = with_retries(transcribe, audio)
    if not text:
        raise EmptyTranscription()
    update_task(job["task_id"], text=text, transcription_status="done")
    edit_message(job["chat_id"], job["message_id"], f"✅ Task #{job['task_id']}: {text}")


def send_notification(chat_id, text):
    tg("sendMessage", chat_id=chat_id, text=text)


def handle_notification(job):
    label = STATUS_LABELS.get(job["status"], job["status"])
    with_retries(send_notification, job["chat_id"], f"📌 Task #{job['task_id']} → {label}")


def main():
    log.info("Worker started, waiting for jobs...")
    while True:
        try:
            item = r.brpop([QUEUE, NOTIFY_QUEUE], timeout=5)
        except redis.RedisError:
            log.error("Redis unavailable, retrying in 3 seconds")
            time.sleep(3)
            continue
        if not item:
            continue

        queue_name, raw = item
        job = json.loads(raw)

        if queue_name == NOTIFY_QUEUE:
            try:
                handle_notification(job)
            except Exception as e:
                log.error("Notification failed: %s", safe(e))
            continue

        log.info("Processing task %s", job["task_id"])
        try:
            handle_job(job)
        except EmptyTranscription:
            fail(job, "I couldn't hear any speech in that voice message.")
        except Exception as e:
            log.error("Job failed: %s", safe(e))
            fail(job, "Sorry, I couldn't transcribe that message. Please try again.")


if __name__ == "__main__":
    main()