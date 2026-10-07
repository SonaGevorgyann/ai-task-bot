from typing import Optional
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Task

Base.metadata.create_all(bind=engine)  # creates the table if it doesn't exist

app = FastAPI(title="AI Task Bot API")

# Lets the React dashboard (a different address) call this API
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


class TaskUpdate(BaseModel):
    status: str


class TaskOut(BaseModel):
    id: int
    text: Optional[str]
    status: str
    source: str
    created_at: datetime

    class Config:
        from_attributes = True


@app.post("/tasks", response_model=TaskOut)
def create_task(data: TaskCreate, db: Session = Depends(get_db)):
    task = Task(text=data.text, source=data.source, telegram_chat_id=data.telegram_chat_id)
    db.add(task)
    db.commit()
    db.refresh(task)
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
    task.status = data.status
    db.commit()
    db.refresh(task)
    return task