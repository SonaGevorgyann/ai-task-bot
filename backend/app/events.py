import os
import json
import logging
import redis

log = logging.getLogger("events")

CHANNEL = "task_events"
NOTIFY_QUEUE = "notification_jobs"
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

_redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def task_to_dict(task):
    user = task.user
    created_at = task.created_at.isoformat() if task.created_at else None
    return {
        "id": task.id,
        "number": task.number,
        "text": task.text,
        "status": task.status,
        "source": task.source,
        "transcription_status": task.transcription_status,
        "created_at": created_at,
        "user": None if user is None else {
            "telegram_id": user.telegram_id,
            "username": user.username,
            "first_name": user.first_name,
        },
    }


def redis_ok():
    try:
        return bool(_redis.ping())
    except redis.RedisError:
        return False


def publish_event(event_type, task):
    """Best-effort: a Redis hiccup must never break task creation or updates."""
    try:
        _redis.publish(CHANNEL, json.dumps({"type": event_type, "task": task_to_dict(task)}))
    except redis.RedisError:
        log.warning("Could not publish %s event", event_type)


def enqueue_notification(task):
    """Best-effort: never break the API call."""
    if not task.telegram_chat_id:
        return
    job = {
        "task_id": task.id,
        "number": task.number or task.id,
        "chat_id": task.telegram_chat_id,
        "status": task.status,
    }
    try:
        _redis.lpush(NOTIFY_QUEUE, json.dumps(job))
    except redis.RedisError:
        log.warning("Could not enqueue notification for task %s", task.id)