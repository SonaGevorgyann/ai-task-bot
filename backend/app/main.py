from typing import Optional
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from .database import ensure_schema, get_db
from .events import CHANNEL, REDIS_HOST, REDIS_PORT, enqueue_notification, publish_event, redis_ok
from .models import Task, User

ensure_schema()

app = FastAPI(title="AI Task Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_STATUSES = {"pending", "in_progress", "completed"}
VALID_SOURCES = {"text", "voice"}
VALID_TRANSCRIPTION = {"processing", "done", "failed"}


class TaskCreate(BaseModel):
    text: str
    source: str = "text"
    telegram_chat_id: Optional[int] = None
    transcription_status: Optional[str] = None
    telegram_id: Optional[int] = None
    username: Optional[str] = None
    first_name: Optional[str] = None


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    text: Optional[str] = None
    transcription_status: Optional[str] = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    text: Optional[str]
    status: str
    source: str
    transcription_status: Optional[str] = None
    created_at: datetime
    user: Optional[UserOut] = None


def upsert_user(db: Session, data: TaskCreate):
    if data.telegram_id is None:
        return None
    user = db.query(User).filter(User.telegram_id == data.telegram_id).one_or_none()
    if user:
        user.username = data.username
        user.first_name = data.first_name
        return user

    user = User(
        telegram_id=data.telegram_id,
        username=data.username,
        first_name=data.first_name,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        user = db.query(User).filter(User.telegram_id == data.telegram_id).one()
        user.username = data.username
        user.first_name = data.first_name
    return user


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if not redis_ok():
        raise HTTPException(status_code=503, detail="Redis unavailable")
    return {"status": "ok"}


@app.post("/tasks", response_model=TaskOut)
def create_task(data: TaskCreate, db: Session = Depends(get_db)):
    if data.source not in VALID_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid source")
    if data.transcription_status is not None and data.transcription_status not in VALID_TRANSCRIPTION:
        raise HTTPException(status_code=400, detail="Invalid transcription status")

    text_value = (data.text or "").strip()
    if data.source == "text" and not text_value:
        raise HTTPException(status_code=400, detail="Task text is empty")

    user = upsert_user(db, data)
    task = Task(
        text=text_value,
        source=data.source,
        telegram_chat_id=data.telegram_chat_id,
        transcription_status=data.transcription_status,
        user=user,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    publish_event("task_created", task)
    return task


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(telegram_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Task).options(joinedload(Task.user)).order_by(Task.created_at.desc())
    if telegram_id is not None:
        query = query.filter(Task.user.has(User.telegram_id == telegram_id))
    return query.all()


@app.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(task_id: int, data: TaskUpdate, db: Session = Depends(get_db)):
    if data.status is None and data.text is None and data.transcription_status is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if data.status is not None and data.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    if data.transcription_status is not None and data.transcription_status not in VALID_TRANSCRIPTION:
        raise HTTPException(status_code=400, detail="Invalid transcription status")

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    status_changed = data.status is not None and task.status != data.status
    if data.status is not None:
        task.status = data.status
    if data.text is not None:
        task.text = data.text
    if data.transcription_status is not None:
        task.transcription_status = data.transcription_status

    db.commit()
    db.refresh(task)
    publish_event("task_updated", task)
    if status_changed:
        enqueue_notification(task)
    return task


@app.get("/events")
async def events(request: Request):
    async def stream():
        client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            while not await request.is_disconnected():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if message:
                    yield f"data: {message['data']}\n\n"
                else:
                    yield ": keepalive\n\n"
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await pubsub.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
