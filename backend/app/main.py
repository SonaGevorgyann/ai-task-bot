from typing import Optional
from datetime import datetime
import redis.asyncio as aioredis
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Task
from .events import (
    CHANNEL, REDIS_HOST, REDIS_PORT,
    publish_event, enqueue_notification,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="AI Task Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_STATUSES = {"pending", "in_progress", "completed"}


class TaskCreate(BaseModel):
    text: str
    source: str = "text"
    telegram_chat_id: Optional[int] = None
    transcription_status: Optional[str] = None


class TaskUpdate(BaseModel):
    status: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    text: Optional[str]
    status: str
    source: str
    transcription_status: Optional[str] = None
    created_at: datetime


@app.post("/tasks", response_model=TaskOut)
def create_task(data: TaskCreate, db: Session = Depends(get_db)):
    task = Task(
        text=data.text,
        source=data.source,
        telegram_chat_id=data.telegram_chat_id,
        transcription_status=data.transcription_status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    publish_event("task_created", task)
    return task


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(db: Session = Depends(get_db)):
    return db.query(Task).order_by(Task.created_at.desc()).all()


@app.patch("/tasks/{task_id}", response_model=TaskOut)
def update_status(task_id: int, data: TaskUpdate, db: Session = Depends(get_db)):
    if data.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    changed = task.status != data.status
    task.status = data.status
    db.commit()
    db.refresh(task)
    publish_event("task_updated", task)
    if changed:
        enqueue_notification(task)
    return task


@app.get("/events")
async def events(request: Request):
    async def stream():
        r = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            while not await request.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if msg:
                    yield f"data: {msg['data']}\n\n"
                else:
                    yield ": keepalive\n\n"
        finally:
            await pubsub.unsubscribe(CHANNEL)
            await pubsub.aclose()
            await r.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )