from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, BigInteger
from .database import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    text = Column(Text, nullable=True)
    status = Column(String, default="pending")   # pending / in_progress / completed
    source = Column(String, default="text")      # text / voice
    telegram_chat_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)