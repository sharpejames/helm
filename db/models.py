from sqlalchemy import Column, String, Boolean, DateTime, Integer, Float, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime
import uuid

class Base(DeclarativeBase):
    pass

class Task(Base):
    __tablename__ = "tasks"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    description = Column(Text)
    type = Column(String, nullable=False)  # manual | scheduled | interval
    schedule = Column(String)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    runs = relationship("TaskRun", back_populates="task", cascade="all, delete-orphan")

class TaskRun(Base):
    __tablename__ = "task_runs"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    status = Column(String, nullable=False)  # pending | running | completed | failed
    input = Column(Text)
    output = Column(Text)
    error = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
    task = relationship("Task", back_populates="runs")
    artifacts = relationship("Artifact", back_populates="run", cascade="all, delete-orphan")

class Artifact(Base):
    __tablename__ = "artifacts"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = Column(String, ForeignKey("task_runs.id"))
    type = Column(String, nullable=False)  # screenshot | url | file | text
    value = Column(Text, nullable=False)
    label = Column(String)
    step = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    run = relationship("TaskRun", back_populates="artifacts")


class VideoSession(Base):
    __tablename__ = "video_sessions"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    region = Column(Text)  # JSON string: {"x", "y", "width", "height"}
    fps = Column(Float, nullable=False, default=1.0)
    conditions = Column(Text)  # comma-separated watch conditions
    status = Column(String, nullable=False, default="stopped")  # running | stopped
    started_at = Column(DateTime, default=datetime.utcnow)
    stopped_at = Column(DateTime)
    alerts = relationship("Alert", back_populates="session", cascade="all, delete-orphan")


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, ForeignKey("video_sessions.id"), nullable=False)
    timestamp = Column(Float, nullable=False)  # Unix timestamp from time.time()
    condition = Column(String, nullable=False)
    description = Column(Text)
    frame_b64 = Column(Text)  # base64-encoded frame thumbnail
    batched_conditions = Column(Text)  # comma-separated if batched
    session = relationship("VideoSession", back_populates="alerts")


class Skill(Base):
    __tablename__ = "skills"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    app = Column(String, nullable=False)
    steps = Column(Text)  # JSON string: [{"action", "params"}, ...]
    created_at = Column(DateTime, default=datetime.utcnow)
