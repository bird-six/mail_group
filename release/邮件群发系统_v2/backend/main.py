from __future__ import annotations

import asyncio
import random
import json
import mimetypes
import os
import smtplib
import sqlite3
import ssl
import threading
import time
import urllib.parse
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from itertools import cycle
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator


class SenderConfig(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str = ""
    email: EmailStr
    auth_code: str = Field(min_length=1, description="QQ 邮箱 SMTP 授权码")
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    enabled: bool = True


class RecipientItem(BaseModel):
    email: EmailStr
    name: str = ""


class MailContent(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    content_type: Literal["plain", "html"] = "plain"


class SendTaskRequest(BaseModel):
    content: MailContent
    senders: list[SenderConfig]
    recipients: list[RecipientItem]

    @field_validator("senders")
    @classmethod
    def has_enabled_sender(cls, value: list[SenderConfig]) -> list[SenderConfig]:
        if not any(sender.enabled for sender in value):
            raise ValueError("至少需要一个启用的发送邮箱")
        return value

    @field_validator("recipients")
    @classmethod
    def has_recipient(cls, value: list[RecipientItem]) -> list[RecipientItem]:
        if not value:
            raise ValueError("至少需要一个收件邮箱")
        return value


class Assignment(BaseModel):
    recipient: str
    sender: str
    status: Literal["pending", "sending", "success", "failed"] = "pending"
    message: str = ""
    finished_at: str | None = None


class TaskSummary(BaseModel):
    task_id: str
    status: Literal["pending", "running", "completed"]
    total: int
    success: int
    failed: int
    pending: int
    progress: float
    created_at: str
    updated_at: str
    assignments: list[Assignment]


class SavedMailOut(BaseModel):
    id: int
    subject: str
    body: str
    created_at: str
    attachments: list[str] = []


class SavedMailCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)


class StoredSender(BaseModel):
    id: int
    email: str
    auth_code: str
    smtp_host: str
    smtp_port: int
    note: str = ""
    enabled: bool = True
    created_at: str


class StoredSenderCreate(BaseModel):
    email: str = Field(min_length=1)
    auth_code: str = Field(min_length=1)
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    note: str = ""


class StoredRecipient(BaseModel):
    id: int
    email: str
    enabled: bool = True
    note: str = ""
    created_at: str


class StoredRecipientCreate(BaseModel):
    email: str = Field(min_length=1)
    note: str = ""


class StoredRecipientUpdate(BaseModel):
    email: str | None = None
    note: str | None = None


class BatchToggleRequest(BaseModel):
    enabled: bool



class StatItem(BaseModel):
    label: str
    total: int
    success: int
    failed: int


class StatsResponse(BaseModel):
    stats: list[StatItem]
    total_success: int
    total_failed: int
    success_rate: float
    total_prev: int = 0
    success_prev: int = 0
    failed_prev: int = 0
    rate_prev: float = 0.0


class SenderBreakdown(BaseModel):
    sender: str
    total: int
    success: int
    failed: int
    success_rate: float


class RecipientBreakdown(BaseModel):
    recipient: str
    note: str
    total: int
    success: int
    failed: int


class FailedRecipient(BaseModel):
    recipient: str
    sender: str
    message: str
    finished_at: str


class FailedReason(BaseModel):
    reason: str
    count: int
    percentage: float


@dataclass
class MailAttachment:
    filename: str
    content: bytes
    content_type: str


@dataclass
class RuntimeTask:
    task_id: str
    content: MailContent
    senders: dict[str, SenderConfig]
    assignments: list[Assignment]
    attachments: list[MailAttachment]
    status: Literal["pending", "running", "completed"]
    created_at: str
    updated_at: str


app = FastAPI(title="邮件群发系统", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TASKS: dict[str, RuntimeTask] = {}
TASK_LOCK = asyncio.Lock()
MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024
BLOCKED_ATTACHMENT_EXTENSIONS = {".exe", ".bat", ".cmd", ".com", ".scr", ".js", ".vbs", ".msi", ".jar"}

def _app_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _frontend_dir() -> str:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
    if os.path.isdir(base):
        return base
    return ""


BASE_DIR = _app_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
SAVED_ATTACHMENTS_DIR = os.path.join(DATA_DIR, "saved_attachments")
DB_PATH = os.path.join(DATA_DIR, "mail_group.sqlite3")


_shared_conn: sqlite3.Connection | None = None
_db_lock = threading.Lock()


def _ensure_conn() -> sqlite3.Connection:
    global _shared_conn
    if _shared_conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _shared_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _shared_conn.row_factory = sqlite3.Row
        _shared_conn.execute("PRAGMA journal_mode=WAL")
    return _shared_conn


@contextmanager
def db():
    with _db_lock:
        yield _ensure_conn()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_mails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        try:
            conn.execute("ALTER TABLE saved_mails ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sender_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                auth_code TEXT NOT NULL,
                smtp_host TEXT NOT NULL DEFAULT 'smtp.qq.com',
                smtp_port INTEGER NOT NULL DEFAULT 465,
                note TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                recipient TEXT NOT NULL,
                sender TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                subject TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(recipients)")]
        if "enabled" not in columns:
            conn.execute("ALTER TABLE recipients ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        if "note" not in columns:
            conn.execute("ALTER TABLE recipients ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            conn.commit()
        sl_columns = [row["name"] for row in conn.execute("PRAGMA table_info(send_log)")]
        if "message" not in sl_columns:
            conn.execute("ALTER TABLE send_log ADD COLUMN message TEXT NOT NULL DEFAULT ''")
            conn.commit()


init_db()


def record_send_log(task_id: str, recipient: str, sender: str, status: str, message: str, finished_at: str) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO send_log (task_id, recipient, sender, status, message, finished_at) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, recipient, sender, status, message, finished_at),
            )
            conn.commit()
    except Exception:
        pass


def disable_recipient(email: str) -> None:
    try:
        with db() as conn:
            conn.execute("UPDATE recipients SET enabled = 0 WHERE email = ?", (normalize_email(email),))
            conn.commit()
    except Exception:
        pass


def save_task_history(summary: TaskSummary, subject: str = "") -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_history (task_id, subject, total, success, failed, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (summary.task_id, subject, summary.total, summary.success, summary.failed, summary.created_at, summary.updated_at),
            )
            conn.commit()
    except Exception:
        pass


def load_task_history(limit: int = 50) -> list[TaskSummary]:
    with db() as conn:
        rows = conn.execute(
            "SELECT task_id, subject, total, success, failed, created_at, updated_at FROM task_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: list[TaskSummary] = []
        for row in rows:
            done = row["success"] + row["failed"]
            total = row["total"]
            progress = round((done / total) * 100, 2) if total else 100.0
            pending = total - done
            result.append(TaskSummary(
                task_id=row["task_id"],
                status="completed",
                total=total,
                success=row["success"],
                failed=row["failed"],
                pending=pending,
                progress=progress,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                assignments=[],
            ))
        return result


def normalize_email(email: str) -> str:
    return email.strip().lower()


def build_assignments(senders: list[SenderConfig], recipients: list[RecipientItem]) -> list[Assignment]:
    enabled_senders = [sender for sender in senders if sender.enabled]
    unique_recipients: list[str] = []
    seen: set[str] = set()

    for recipient in recipients:
        email = normalize_email(str(recipient.email))
        if email not in seen:
            seen.add(email)
            unique_recipients.append(email)

    sender_cycle = cycle(enabled_senders)
    return [
        Assignment(recipient=email, sender=str(next(sender_cycle).email))
        for email in unique_recipients
    ]


def to_summary(task: RuntimeTask) -> TaskSummary:
    success = sum(1 for item in task.assignments if item.status == "success")
    failed = sum(1 for item in task.assignments if item.status == "failed")
    pending = sum(1 for item in task.assignments if item.status in {"pending", "sending"})
    total = len(task.assignments)
    done = success + failed
    progress = round((done / total) * 100, 2) if total else 100.0
    return TaskSummary(
        task_id=task.task_id,
        status=task.status,
        total=total,
        success=success,
        failed=failed,
        pending=pending,
        progress=progress,
        created_at=task.created_at,
        updated_at=task.updated_at,
        assignments=task.assignments,
    )


def send_email(sender: SenderConfig, recipient: str, content: MailContent, attachments: list[MailAttachment]) -> None:
    message = EmailMessage()
    message["From"] = f"{sender.name} <{sender.email}>" if sender.name else str(sender.email)
    message["To"] = recipient
    message["Subject"] = content.subject
    message.set_content(content.body, subtype="plain", charset="utf-8")

    for attachment in attachments:
        maintype, subtype = attachment.content_type.split("/", 1) if "/" in attachment.content_type else ("application", "octet-stream")
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(sender.smtp_host, sender.smtp_port, context=context, timeout=30) as smtp:
        smtp.login(str(sender.email), sender.auth_code)
        smtp.send_message(message)


async def parse_attachments(files: list[UploadFile]) -> list[MailAttachment]:
    attachments: list[MailAttachment] = []
    for file in files:
        filename = file.filename or "attachment"
        lowered = filename.lower()
        if any(lowered.endswith(extension) for extension in BLOCKED_ATTACHMENT_EXTENSIONS):
            raise HTTPException(status_code=400, detail=f"{filename} 是 QQ 邮箱不建议发送的附件类型")
        content = await file.read()
        if len(content) > MAX_ATTACHMENT_SIZE:
            raise HTTPException(status_code=400, detail=f"{filename} 超过 50MB 限制")
        content_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        attachments.append(MailAttachment(filename=filename, content=content, content_type=content_type))
    return attachments


async def run_task(task_id: str) -> None:
    task = TASKS[task_id]
    task.status = "running"
    task.updated_at = datetime.now().isoformat(timespec="seconds")

    async def send_one(index: int, assignment: Assignment) -> None:
        sender = task.senders[assignment.sender]
        assignment.status = "sending"
        task.updated_at = datetime.now().isoformat(timespec="seconds")
        try:
            await asyncio.to_thread(send_email, sender, assignment.recipient, task.content, task.attachments)
            assignment.status = "success"
            assignment.message = "发送成功"
            disable_recipient(assignment.recipient)
        except Exception as exc:
            assignment.status = "failed"
            assignment.message = str(exc)
        finally:
            finished_at = datetime.now().isoformat(timespec="seconds")
            assignment.finished_at = finished_at
            task.assignments[index] = assignment
            task.updated_at = finished_at
            record_send_log(task_id, assignment.recipient, assignment.sender, assignment.status, assignment.message, finished_at)

    async def sender_group(sender_assignments: list[tuple[int, Assignment]]) -> None:
        for idx, (index, assignment) in enumerate(sender_assignments):
            if idx > 0:
                delay = random.uniform(20, 40)
                await asyncio.sleep(delay)
            await send_one(index, assignment)

    groups: dict[str, list[tuple[int, Assignment]]] = {}
    for index, assignment in enumerate(task.assignments):
        groups.setdefault(assignment.sender, []).append((index, assignment))

    await asyncio.gather(
        *(sender_group(sender_assignments) for sender_assignments in groups.values())
    )

    task.status = "completed"
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    save_task_history(to_summary(task), task.content.subject)

    async with TASK_LOCK:
        TASKS.pop(task_id, None)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tasks", response_model=TaskSummary)
async def create_task(
    payload: str = Form(...),
    attachments: list[UploadFile] = File(default=[]),
) -> TaskSummary:
    try:
        parsed_payload = SendTaskRequest.model_validate_json(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="任务参数格式错误") from exc

    parsed_attachments = await parse_attachments(attachments)
    assignments = build_assignments(parsed_payload.senders, parsed_payload.recipients)
    if not assignments:
        raise HTTPException(status_code=400, detail="没有有效收件邮箱")

    task_id = uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    sender_map = {str(sender.email): sender for sender in parsed_payload.senders if sender.enabled}
    task = RuntimeTask(
        task_id=task_id,
        content=parsed_payload.content,
        senders=sender_map,
        assignments=assignments,
        attachments=parsed_attachments,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    async with TASK_LOCK:
        TASKS[task_id] = task

    asyncio.create_task(run_task(task_id))
    return to_summary(task)


@app.get("/api/tasks", response_model=list[TaskSummary])
async def list_tasks() -> list[TaskSummary]:
    db_tasks = load_task_history()
    async with TASK_LOCK:
        in_memory_ids = {task.task_id for task in TASKS.values()}
        result = [to_summary(task) for task in sorted(TASKS.values(), key=lambda item: item.created_at, reverse=True)]
    result.extend(task for task in db_tasks if task.task_id not in in_memory_ids)
    return result


@app.get("/api/tasks/{task_id}", response_model=TaskSummary)
async def get_task(task_id: str) -> TaskSummary:
    async with TASK_LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return to_summary(task)


@app.post("/api/assignments/preview", response_model=list[Assignment])
def preview_assignments(payload: SendTaskRequest) -> list[Assignment]:
    return build_assignments(payload.senders, payload.recipients)


# ── 保存邮件 ──────────────────────────────────────────────────────────────


@app.post("/api/saved-mails", response_model=SavedMailOut)
async def save_mail(
    subject: str = Form(...),
    body: str = Form(...),
    attachments: list[UploadFile] = File(default=[]),
) -> SavedMailOut:
    now = datetime.now().isoformat(timespec="seconds")
    attachment_names: list[str] = []
    os.makedirs(SAVED_ATTACHMENTS_DIR, exist_ok=True)
    for file in attachments:
        filename = file.filename or "attachment"
        safe_name = f"{uuid4().hex}_{filename}"
        filepath = os.path.join(SAVED_ATTACHMENTS_DIR, safe_name)
        content = await file.read()
        with open(filepath, "wb") as f:
            f.write(content)
        attachment_names.append(safe_name)

    with db() as conn:
        attachments_json = json.dumps(attachment_names)
        cursor = conn.execute(
            "INSERT INTO saved_mails (subject, body, created_at, attachments) VALUES (?, ?, ?, ?)",
            (subject, body, now, attachments_json),
        )
        conn.commit()
        return SavedMailOut(id=cursor.lastrowid, subject=subject, body=body, created_at=now, attachments=attachment_names)


@app.get("/api/saved-mails", response_model=list[SavedMailOut])
def list_saved_mails() -> list[SavedMailOut]:
    with db() as conn:
        rows = conn.execute("SELECT id, subject, body, created_at, attachments FROM saved_mails ORDER BY id DESC").fetchall()
        result: list[SavedMailOut] = []
        for row in rows:
            atts = json.loads(row["attachments"]) if row["attachments"] else []
            result.append(SavedMailOut(id=row["id"], subject=row["subject"], body=row["body"], created_at=row["created_at"], attachments=atts))
        return result


@app.get("/api/saved-mails/{mail_id}", response_model=SavedMailOut)
def get_saved_mail(mail_id: int) -> SavedMailOut:
    with db() as conn:
        row = conn.execute("SELECT id, subject, body, created_at, attachments FROM saved_mails WHERE id = ?", (mail_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="保存的邮件不存在")
        atts = json.loads(row["attachments"]) if row["attachments"] else []
        return SavedMailOut(id=row["id"], subject=row["subject"], body=row["body"], created_at=row["created_at"], attachments=atts)


@app.get("/api/saved-mails/{mail_id}/attachments/{filename}")
def download_saved_attachment(mail_id: int, filename: str) -> Response:
    with db() as conn:
        row = conn.execute("SELECT attachments FROM saved_mails WHERE id = ?", (mail_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="保存的邮件不存在")
        atts = json.loads(row["attachments"]) if row["attachments"] else []
        if filename not in atts:
            raise HTTPException(status_code=404, detail="附件不存在")
    safe_name = os.path.basename(filename)
    filepath = os.path.join(SAVED_ATTACHMENTS_DIR, safe_name)
    real_filepath = os.path.realpath(filepath)
    real_saved_dir = os.path.realpath(SAVED_ATTACHMENTS_DIR)
    if not real_filepath.startswith(real_saved_dir + os.sep):
        raise HTTPException(status_code=403, detail="非法的文件路径")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="附件文件不存在")
    original_name = filename.split("_", 1)[1] if "_" in filename else filename
    encoded_name = urllib.parse.quote(original_name)
    content_type, _ = mimetypes.guess_type(original_name)
    return Response(
        content=open(filepath, "rb").read(),
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@app.delete("/api/saved-mails/{mail_id}")
def delete_saved_mail(mail_id: int) -> dict[str, str]:
    with db() as conn:
        row = conn.execute("SELECT attachments FROM saved_mails WHERE id = ?", (mail_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="保存的邮件不存在")
        atts = json.loads(row["attachments"]) if row["attachments"] else []
        for filename in atts:
            filepath = os.path.join(SAVED_ATTACHMENTS_DIR, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
        conn.execute("DELETE FROM saved_mails WHERE id = ?", (mail_id,))
        conn.commit()
        return {"status": "ok", "message": "删除成功"}


# ── 发送邮箱 ──────────────────────────────────────────────────────────────


@app.get("/api/sender-configs", response_model=list[StoredSender])
def list_sender_configs() -> list[StoredSender]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, email, auth_code, smtp_host, smtp_port, note, enabled, created_at FROM sender_configs ORDER BY id DESC"
        ).fetchall()
        return [
            StoredSender(
                id=row["id"], email=row["email"], auth_code=row["auth_code"],
                smtp_host=row["smtp_host"], smtp_port=row["smtp_port"],
                note=row["note"], enabled=bool(row["enabled"]), created_at=row["created_at"],
            )
            for row in rows
        ]


@app.post("/api/sender-configs", response_model=StoredSender)
def create_sender_config(payload: StoredSenderCreate) -> StoredSender:
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO sender_configs (email, auth_code, smtp_host, smtp_port, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (payload.email, payload.auth_code, payload.smtp_host, payload.smtp_port, payload.note, now),
        )
        conn.commit()
        return StoredSender(
            id=cursor.lastrowid, email=payload.email, auth_code=payload.auth_code,
            smtp_host=payload.smtp_host, smtp_port=payload.smtp_port,
            note=payload.note, enabled=True, created_at=now,
        )


@app.put("/api/sender-configs/{sender_id}", response_model=StoredSender)
def update_sender_config(sender_id: int, payload: StoredSenderCreate) -> StoredSender:
    with db() as conn:
        row = conn.execute("SELECT id, enabled, created_at FROM sender_configs WHERE id = ?", (sender_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="发送邮箱不存在")
        conn.execute(
            "UPDATE sender_configs SET email=?, auth_code=?, smtp_host=?, smtp_port=?, note=? WHERE id=?",
            (payload.email, payload.auth_code, payload.smtp_host, payload.smtp_port, payload.note, sender_id),
        )
        conn.commit()
        return StoredSender(
            id=sender_id, email=payload.email, auth_code=payload.auth_code,
            smtp_host=payload.smtp_host, smtp_port=payload.smtp_port,
            note=payload.note, enabled=bool(row["enabled"]), created_at=row["created_at"],
        )


@app.delete("/api/sender-configs/{sender_id}")
def delete_sender_config(sender_id: int) -> dict[str, str]:
    with db() as conn:
        cursor = conn.execute("DELETE FROM sender_configs WHERE id = ?", (sender_id,))
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="发送邮箱不存在")
        return {"status": "ok", "message": "删除成功"}


@app.patch("/api/sender-configs/{sender_id}/toggle")
def toggle_sender_config(sender_id: int) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute("SELECT enabled FROM sender_configs WHERE id = ?", (sender_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="发送邮箱不存在")
        new_enabled = 0 if row["enabled"] else 1
        conn.execute("UPDATE sender_configs SET enabled=? WHERE id=?", (new_enabled, sender_id))
        conn.commit()
        return {"enabled": bool(new_enabled)}


# ── 收件人 ────────────────────────────────────────────────────────────────


@app.get("/api/recipients", response_model=list[StoredRecipient])
def list_recipients() -> list[StoredRecipient]:
    with db() as conn:
        rows = conn.execute("SELECT id, email, enabled, note, created_at FROM recipients ORDER BY id DESC").fetchall()
        return [StoredRecipient(id=row["id"], email=row["email"], enabled=bool(row["enabled"]), note=row["note"], created_at=row["created_at"]) for row in rows]


@app.post("/api/recipients", response_model=StoredRecipient)
def create_recipient(payload: StoredRecipientCreate) -> StoredRecipient:
    now = datetime.now().isoformat(timespec="seconds")
    email = normalize_email(payload.email)
    note = payload.note or ""
    with db() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO recipients (email, enabled, note, created_at) VALUES (?, 1, ?, ?)",
            (email, note, now),
        )
        conn.commit()
        if cursor.rowcount == 0:
            row = conn.execute("SELECT id, email, enabled, note, created_at FROM recipients WHERE email=?", (email,)).fetchone()
            return StoredRecipient(id=row["id"], email=row["email"], enabled=bool(row["enabled"]), note=row["note"], created_at=row["created_at"])
        return StoredRecipient(id=cursor.lastrowid, email=email, enabled=True, note=note, created_at=now)


@app.post("/api/recipients/batch")
def batch_create_recipients(payload: list[StoredRecipientCreate]) -> list[StoredRecipient]:
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        results: list[StoredRecipient] = []
        for item in payload:
            email = normalize_email(item.email)
            if not email:
                continue
            note = item.note or ""
            cursor = conn.execute(
                "INSERT OR IGNORE INTO recipients (email, enabled, note, created_at) VALUES (?, 1, ?, ?)",
                (email, note, now),
            )
            if cursor.rowcount > 0:
                results.append(StoredRecipient(id=cursor.lastrowid, email=email, enabled=True, note=note, created_at=now))
            else:
                row = conn.execute("SELECT id, email, enabled, note, created_at FROM recipients WHERE email=?", (email,)).fetchone()
                if row:
                    results.append(StoredRecipient(id=row["id"], email=row["email"], enabled=bool(row["enabled"]), note=row["note"], created_at=row["created_at"]))
        conn.commit()
        return results


@app.delete("/api/recipients/{recipient_id}")
def delete_recipient(recipient_id: int) -> dict[str, str]:
    with db() as conn:
        cursor = conn.execute("DELETE FROM recipients WHERE id = ?", (recipient_id,))
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="收件人不存在")
        return {"status": "ok", "message": "删除成功"}


@app.patch("/api/recipients/{recipient_id}/toggle")
def toggle_recipient(recipient_id: int) -> StoredRecipient:
    with db() as conn:
        row = conn.execute("SELECT id, email, enabled, note, created_at FROM recipients WHERE id=?", (recipient_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="收件人不存在")
        new_enabled = 0 if row["enabled"] else 1
        conn.execute("UPDATE recipients SET enabled=? WHERE id=?", (new_enabled, recipient_id))
        conn.commit()
        return StoredRecipient(id=row["id"], email=row["email"], enabled=bool(new_enabled), note=row["note"], created_at=row["created_at"])


@app.post("/api/recipients/batch-toggle")
def batch_toggle_recipients(payload: BatchToggleRequest) -> dict[str, str]:
    enabled_val = 1 if payload.enabled else 0
    with db() as conn:
        conn.execute("UPDATE recipients SET enabled = ?", (enabled_val,))
        conn.commit()
    action = "启用" if payload.enabled else "禁用"
    return {"status": "ok", "message": f"全部收件人已{action}"}


@app.patch("/api/recipients/{recipient_id}", response_model=StoredRecipient)
def update_recipient(recipient_id: int, payload: StoredRecipientUpdate) -> StoredRecipient:
    with db() as conn:
        row = conn.execute("SELECT id, email, enabled, note, created_at FROM recipients WHERE id=?", (recipient_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="收件人不存在")
        new_email = normalize_email(payload.email) if payload.email is not None else row["email"]
        new_note = payload.note if payload.note is not None else row["note"]
        conn.execute("UPDATE recipients SET email=?, note=? WHERE id=?", (new_email, new_note, recipient_id))
        conn.commit()
        return StoredRecipient(id=row["id"], email=new_email, enabled=bool(row["enabled"]), note=new_note, created_at=row["created_at"])


# ── 统计数据 ──────────────────────────────────────────────────────────────


def _previous_period_range(period: str) -> tuple[str, str]:
    now = datetime.now()
    if period == "hour":
        prev_end = now.replace(minute=0, second=0, microsecond=0)
        prev_start = prev_end - timedelta(hours=24)
    elif period == "day":
        prev_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        if now.month == 1:
            prev_start = prev_start.replace(year=now.year - 1, month=12)
        else:
            prev_start = prev_start.replace(month=prev_start.month - 1)
        import calendar
        prev_end = prev_start.replace(day=calendar.monthrange(prev_start.year, prev_start.month)[1])
    elif period == "week":
        current_weekday = now.weekday()
        prev_end = (now.replace(hour=23, minute=59, second=59, microsecond=0)
                    - __import__('datetime').timedelta(days=current_weekday + 1))
        prev_start = prev_end - __import__('datetime').timedelta(days=6)
        prev_start = prev_start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        prev_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - __import__('datetime').timedelta(days=1)
        prev_end = prev_end.replace(hour=23, minute=59, second=59)
        prev_start = prev_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return "", ""
    return prev_start.isoformat(timespec="seconds"), prev_end.isoformat(timespec="seconds")


def _count_period(conn: sqlite3.Connection, start: str, end: str) -> tuple[int, int]:
    if not start or not end:
        return 0, 0
    row = conn.execute(
        "SELECT SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as s,"
        "       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as f"
        " FROM send_log WHERE finished_at >= ? AND finished_at <= ?",
        (start, end),
    ).fetchone()
    return (row["s"] or 0, row["f"] or 0)


@app.get("/api/stats", response_model=StatsResponse)
def get_stats(period: str = "hour") -> StatsResponse:
    from datetime import timedelta
    with db() as conn:
        summary = conn.execute(
            "SELECT SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as total_success,"
            "       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as total_failed,"
            "       COUNT(*) as total_all"
            " FROM send_log"
        ).fetchone()
        total_success = summary["total_success"] or 0
        total_failed = summary["total_failed"] or 0
        total_all = summary["total_all"] or 0

        if period == "day":
            sql = """
                SELECT strftime('%m-%d', finished_at) as label,
                       COUNT(*) as total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM send_log
                GROUP BY label
                ORDER BY label DESC
                LIMIT 30
            """
        elif period == "week":
            sql = """
                SELECT strftime('%Y-%m', finished_at) || '-' || CAST((CAST(strftime('%d', finished_at) AS INTEGER) - 1) / 7 + 1 AS INTEGER) as label,
                       COUNT(*) as total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM send_log
                GROUP BY strftime('%Y-%m', finished_at) || '-' || CAST((CAST(strftime('%d', finished_at) AS INTEGER) - 1) / 7 + 1 AS INTEGER)
                ORDER BY label DESC
                LIMIT 12
            """
        elif period == "month":
            sql = """
                SELECT strftime('%Y-%m', finished_at) as label,
                       COUNT(*) as total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM send_log
                GROUP BY label
                ORDER BY label DESC
                LIMIT 12
            """
        else:
            sql = """
                SELECT strftime('%m-%d %H:00', finished_at) as label,
                       COUNT(*) as total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
                FROM send_log
                GROUP BY strftime('%Y-%m-%d %H', finished_at)
                ORDER BY label DESC
                LIMIT 24
            """

        rows = conn.execute(sql).fetchall()
        stats = [
            StatItem(label=row["label"], total=row["total"], success=row["success"], failed=row["failed"])
            for row in rows
        ]
        stats.reverse()
        success_rate = round(total_success / total_all * 100, 2) if total_all > 0 else 0.0

        prev_start, prev_end = _previous_period_range(period)
        prev_s, prev_f = _count_period(conn, prev_start, prev_end)
        prev_all = prev_s + prev_f
        rate_prev = round(prev_s / prev_all * 100, 2) if prev_all > 0 else 0.0

        return StatsResponse(
            stats=stats,
            total_success=total_success,
            total_failed=total_failed,
            success_rate=success_rate,
            total_prev=prev_all,
            success_prev=prev_s,
            failed_prev=prev_f,
            rate_prev=rate_prev,
        )


@app.get("/api/stats/sender-breakdown", response_model=list[SenderBreakdown])
def get_sender_breakdown() -> list[SenderBreakdown]:
    with db() as conn:
        rows = conn.execute(
            "SELECT sender,"
            "       COUNT(*) as total,"
            "       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,"
            "       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed"
            " FROM send_log"
            " GROUP BY sender"
            " ORDER BY total DESC"
        ).fetchall()
        return [
            SenderBreakdown(
                sender=row["sender"],
                total=row["total"],
                success=row["success"],
                failed=row["failed"],
                success_rate=round(row["success"] / row["total"] * 100, 2) if row["total"] else 0.0,
            )
            for row in rows
        ]


@app.get("/api/stats/recipient-breakdown", response_model=list[RecipientBreakdown])
def get_recipient_breakdown() -> list[RecipientBreakdown]:
    with db() as conn:
        rows = conn.execute(
            "SELECT sl.recipient,"
            "       COALESCE(r.note, '') as note,"
            "       COUNT(*) as total,"
            "       SUM(CASE WHEN sl.status='success' THEN 1 ELSE 0 END) as success,"
            "       SUM(CASE WHEN sl.status='failed' THEN 1 ELSE 0 END) as failed"
            " FROM send_log sl"
            " LEFT JOIN recipients r ON r.email = sl.recipient"
            " GROUP BY sl.recipient"
            " ORDER BY total DESC"
        ).fetchall()
        return [
            RecipientBreakdown(
                recipient=row["recipient"],
                note=row["note"],
                total=row["total"],
                success=row["success"],
                failed=row["failed"],
            )
            for row in rows
        ]


@app.get("/api/stats/failed-recipients", response_model=list[FailedRecipient])
def get_failed_recipients(page: int = 1, page_size: int = 20) -> list[FailedRecipient]:
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    offset = (page - 1) * page_size
    with db() as conn:
        rows = conn.execute(
            "SELECT recipient, sender, message, finished_at"
            " FROM send_log"
            " WHERE status='failed'"
            " ORDER BY finished_at DESC"
            " LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
        return [
            FailedRecipient(
                recipient=row["recipient"],
                sender=row["sender"],
                message=row["message"],
                finished_at=row["finished_at"],
            )
            for row in rows
        ]


@app.get("/api/stats/failed-recipients/total", response_model=dict)
def get_failed_recipients_total() -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as total FROM send_log WHERE status='failed'"
        ).fetchone()
        return {"total": row["total"] if row else 0}


@app.get("/api/stats/failed-recipients/emails")
def get_failed_recipient_emails() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT recipient FROM send_log WHERE status='failed' ORDER BY recipient"
        ).fetchall()
        return [row["recipient"] for row in rows]


@app.get("/api/stats/failure-reasons", response_model=list[FailedReason])
def get_failure_reasons() -> list[FailedReason]:
    with db() as conn:
        rows = conn.execute(
            "SELECT message as reason, COUNT(*) as count"
            " FROM send_log"
            " WHERE status='failed' AND message != ''"
            " GROUP BY message"
            " ORDER BY count DESC"
        ).fetchall()
        total = sum(row["count"] for row in rows)
        return [
            FailedReason(
                reason=row["reason"],
                count=row["count"],
                percentage=round(row["count"] / total * 100, 1) if total > 0 else 0.0,
            )
            for row in rows
        ]


# ── 静态文件（前端） ──────────────────────────────────────────────────────

frontend_path = _frontend_dir()
if frontend_path:
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")


# ── 入口 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    import socket
    import time as _time

    def _port_is_free(host: str, p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return True
            except OSError:
                return False

    port = 8000
    while port < 8020 and not _port_is_free("127.0.0.1", port):
        print(f"  Port {port} is occupied, trying next...")
        port += 1

    url = f"http://127.0.0.1:{port}"
    log_file = os.path.join(DATA_DIR, "server.log")
    os.makedirs(DATA_DIR, exist_ok=True)

    pid_file = os.path.join(DATA_DIR, "server.pid")
    with open(pid_file, "w") as pf:
        pf.write(str(os.getpid()))

    threading.Thread(
        target=lambda: (_time.sleep(2), webbrowser.open(url)),
        daemon=True,
    ).start()

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {
                    "()": "logging.Formatter",
                    "fmt": "%(asctime)s %(levelname)s %(message)s",
                },
            },
            "handlers": {
                "file": {
                    "class": "logging.FileHandler",
                    "filename": log_file,
                    "formatter": "plain",
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["file"], "level": "INFO"},
                "uvicorn.error": {"handlers": ["file"], "level": "INFO"},
                "uvicorn.access": {"handlers": ["file"], "level": "INFO"},
            },
        },
    )
