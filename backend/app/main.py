import json
import secrets
from typing import Optional
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from .database import SessionLocal, ensure_schema, get_db
from .events import CHANNEL, REDIS_HOST, REDIS_PORT, enqueue_notification, publish_event, redis_ok
from .models import Task, User

def new_board_token():
    return secrets.token_urlsafe(32)


def backfill_board_tokens():
    db = SessionLocal()
    try:
        missing = db.query(User).filter(User.board_token.is_(None)).all()
        for user in missing:
            user.board_token = new_board_token()
        if missing:
            db.commit()
    finally:
        db.close()


ensure_schema()
backfill_board_tokens()

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


class BoardIn(BaseModel):
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None


class BoardOut(BaseModel):
    board_token: str


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


def upsert_user(db: Session, telegram_id, username, first_name):
    if telegram_id is None:
        return None
    user = db.query(User).filter(User.telegram_id == telegram_id).one_or_none()
    if user:
        user.username = username
        user.first_name = first_name
        if not user.board_token:
            user.board_token = new_board_token()
        return user

    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        board_token=new_board_token(),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        user = db.query(User).filter(User.telegram_id == telegram_id).one()
        user.username = username
        user.first_name = first_name
        if not user.board_token:
            user.board_token = new_board_token()
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

    user = upsert_user(db, data.telegram_id, data.username, data.first_name)
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


def user_for_board(db: Session, board: str):
    return db.query(User).filter(User.board_token == board).one_or_none()


@app.post("/boards", response_model=BoardOut)
def open_board(data: BoardIn, db: Session = Depends(get_db)):
    user = upsert_user(db, data.telegram_id, data.username, data.first_name)
    db.commit()
    db.refresh(user)
    return {"board_token": user.board_token}


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(board: Optional[str] = None, db: Session = Depends(get_db)):
    if not board:
        raise HTTPException(status_code=401, detail="Board token required")
    user = user_for_board(db, board)
    if not user:
        raise HTTPException(status_code=404, detail="Unknown board")
    return (
        db.query(Task)
        .options(joinedload(Task.user))
        .filter(Task.user_id == user.id)
        .order_by(Task.created_at.desc())
        .all()
    )


@app.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(
    task_id: int,
    data: TaskUpdate,
    board: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if data.status is None and data.text is None and data.transcription_status is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if data.status is not None and data.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    if data.transcription_status is not None and data.transcription_status not in VALID_TRANSCRIPTION:
        raise HTTPException(status_code=400, detail="Invalid transcription status")

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if board is not None:
        owner = user_for_board(db, board)
        if not owner or task.user_id != owner.id:
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


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(task_id: int, board: str, db: Session = Depends(get_db)):
    owner = user_for_board(db, board)
    task = db.query(Task).options(joinedload(Task.user)).filter(Task.id == task_id).one_or_none()
    if not owner or not task or task.user_id != owner.id:
        raise HTTPException(status_code=404, detail="Task not found")
    publish_event("task_deleted", task)
    db.delete(task)
    db.commit()
    return Response(status_code=204)


@app.get("/events")
async def events(request: Request, board: str, db: Session = Depends(get_db)):
    user = user_for_board(db, board)
    if not user:
        raise HTTPException(status_code=404, detail="Unknown board")
    telegram_id = user.telegram_id

    async def stream():
        client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(CHANNEL)
        try:
            while not await request.is_disconnected():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if message:
                    try:
                        payload = json.loads(message["data"])
                    except json.JSONDecodeError:
                        continue
                    task_user = (payload.get("task") or {}).get("user") or {}
                    if task_user.get("telegram_id") != telegram_id:
                        continue
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
