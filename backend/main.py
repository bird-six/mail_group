from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import json
import mimetypes
import os
import smtplib
import sqlite3
import ssl
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import webbrowser
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from itertools import cycle
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, ValidationError, field_validator


def clean_copied_email(value: object) -> object:
    if not isinstance(value, str):
        return value
    value = value.replace("\u200b", "").replace("\ufeff", "").strip()
    if value.lower().startswith("mailto:"):
        value = value[7:].strip()
    return value


EmailAddress = Annotated[EmailStr, BeforeValidator(clean_copied_email)]


class SenderConfig(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    name: str = ""
    email: EmailAddress
    auth_code: str = Field(min_length=1, description="SMTP 授权码 / 邮箱密码")
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    enabled: bool = True


class RecipientItem(BaseModel):
    email: EmailAddress
    name: str = ""


class MailContent(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    content_type: Literal["plain", "html"] = "plain"


class SendTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: MailContent
    sender_ids: list[Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]] = Field(min_length=1)
    recipients: list[RecipientItem]
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)

    @field_validator("recipients")
    @classmethod
    def has_recipient(cls, value: list[RecipientItem]) -> list[RecipientItem]:
        if not value:
            raise ValueError("至少需要一个收件邮箱")
        return value


class Assignment(BaseModel):
    recipient: str
    sender: str
    status: Literal["pending", "sending", "success", "failed", "uncertain"] = "pending"
    message: str = ""
    finished_at: str | None = None


class TaskSummary(BaseModel):
    task_id: str
    status: Literal["pending", "running", "completed", "interrupted"]
    subject: str = ""
    uncertain: int = 0
    resumable: int = 0
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
    content_type: Literal["plain", "html"] = "plain"
    created_at: str
    attachments: list[str] = []


class SavedMailCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)


class StoredSender(BaseModel):
    id: int
    email: str
    has_auth_code: bool = True
    smtp_host: str
    smtp_port: int
    note: str = ""
    enabled: bool = True
    created_at: str


class StoredSenderCreate(BaseModel):
    email: EmailAddress
    auth_code: str = Field(min_length=1, description="SMTP 授权码 / 邮箱密码")
    smtp_host: str = Field(default="smtp.qq.com", min_length=1)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    note: str = ""


class StoredSenderUpdate(BaseModel):
    email: EmailAddress
    auth_code: str = ""
    smtp_host: str = Field(default="smtp.qq.com", min_length=1)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    note: str = ""


class ResumeRequest(BaseModel):
    retry_uncertain: bool = False


class StoredRecipient(BaseModel):
    id: int
    email: str
    enabled: bool = True
    note: str = ""
    created_at: str


class StoredRecipientCreate(BaseModel):
    email: EmailAddress
    note: str = ""


class StoredRecipientUpdate(BaseModel):
    email: EmailAddress | None = None
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
    period_success: int = 0
    period_failed: int = 0
    period_rate: float = 0.0


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
    status: Literal["pending", "running", "completed", "interrupted"]
    created_at: str
    updated_at: str
    sender_ids: list[int] = field(default_factory=list)
    attachment_names: list[str] = field(default_factory=list)


API_TOKEN = os.environ.get("MAIL_GROUP_API_TOKEN") or secrets.token_urlsafe(32)
DEV_ORIGIN = os.environ.get("MAIL_GROUP_DEV_ORIGIN", "")
STOPPING = False
WORKERS: dict[str, asyncio.Task] = {}
SENDER_LOCKS: dict[str, asyncio.Lock] = {}
SENDER_NEXT_SEND: dict[str, float] = {}
SEND_INTERVAL = (20.0, 40.0)
SERVER = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global STOPPING, _shared_conn
    STOPPING = False
    init_db()
    yield
    STOPPING = True
    if WORKERS:
        await asyncio.gather(*list(WORKERS.values()), return_exceptions=True)
    if _shared_conn is not None:
        _shared_conn.close()
        _shared_conn = None


app = FastAPI(title="邮件群发助手", version="3.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
if DEV_ORIGIN:
    app.add_middleware(CORSMiddleware, allow_origins=[DEV_ORIGIN], allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])


@app.middleware("http")
async def authorize_api(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        trusted = {str(request.base_url).rstrip("/")}
        if DEV_ORIGIN:
            trusted.add(DEV_ORIGIN)
        if origin and origin not in trusted:
            return JSONResponse({"detail": "不允许的请求来源"}, status_code=403)
        if request.method == "OPTIONS" and DEV_ORIGIN and origin == DEV_ORIGIN:
            return await call_next(request)
        if not (request.url.path == "/api/health" and request.method == "GET"):
            expected = f"Bearer {API_TOKEN}"
            if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
                return JSONResponse({"detail": "会话已失效，请重新打开应用"}, status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    return response


def validation_details(exc):
    errors = []
    for error in exc.errors():
        safe = {key: error[key] for key in ("type", "loc", "msg") if key in error}
        if error.get("loc") and error["loc"][-1] == "email" and isinstance(error.get("input"), str):
            safe["input"] = error["input"]
        errors.append(safe)
    return errors


@app.exception_handler(RequestValidationError)
async def request_validation_handler(_request, exc):
    return JSONResponse({"detail": validation_details(exc)}, status_code=422)

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
DATA_DIR = os.environ.get("MAIL_GROUP_DATA_DIR") or (
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MailGroup", "data")
    if getattr(sys, "frozen", False) else os.path.join(BASE_DIR, "data")
)
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
        conn = _ensure_conn()
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise


def init_db() -> None:
    with db() as conn:
        existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sender_configs" in existing_tables and "task_runs" not in existing_tables:
            backup_path = os.path.join(DATA_DIR, "before-v3.sqlite3")
            if not os.path.exists(backup_path):
                backup = sqlite3.connect(backup_path)
                try:
                    conn.backup(backup)
                finally:
                    backup.close()
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
        mail_columns = {row["name"] for row in conn.execute("PRAGMA table_info(saved_mails)")}
        if "content_type" not in mail_columns:
            conn.execute("ALTER TABLE saved_mails ADD COLUMN content_type TEXT NOT NULL DEFAULT 'plain'")
        conn.execute("""CREATE TABLE IF NOT EXISTS task_runs (
            task_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, request_digest TEXT NOT NULL,
            request_json TEXT NOT NULL, assignments TEXT NOT NULL, attachment_names TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_log_task ON send_log(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_log_time ON send_log(finished_at)")
        # An accepted SMTP message cannot be inferred from an interrupted 'sending' state.
        for row in conn.execute("SELECT task_id, assignments FROM task_runs WHERE status IN ('pending','running')").fetchall():
            assignments = json.loads(row["assignments"])
            for item in assignments:
                if item["status"] == "sending":
                    item["status"] = "uncertain"
                    item["message"] = "上次退出时正在发送，请核对是否已送达"
            conn.execute("UPDATE task_runs SET status='interrupted', assignments=? WHERE task_id=?", (json.dumps(assignments), row["task_id"]))
        conn.commit()


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
            # Legacy rows may still contain clipboard artifacts. Disable all
            # equivalent records after success so a reload cannot resend them.
            normalized = normalize_email(email)
            matches = [
                (row["id"],) for row in conn.execute("SELECT id, email FROM recipients WHERE enabled = 1")
                if normalize_email(row["email"]) == normalized
            ]
            conn.executemany("UPDATE recipients SET enabled = 0 WHERE id = ?", matches)
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


def summary_from_row(row) -> TaskSummary:
    content = json.loads(row["request_json"])["content"]
    task = RuntimeTask(row["task_id"], MailContent(**content), {},
        [Assignment(**item) for item in json.loads(row["assignments"])], [],
        row["status"], row["created_at"], row["updated_at"])
    return to_summary(task)


def load_task_history(limit: int | None = None) -> list[TaskSummary]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM task_runs ORDER BY created_at DESC").fetchall()
        result = [summary_from_row(row) for row in rows]
        known = {row["task_id"] for row in rows}
        for row in conn.execute("SELECT * FROM task_history ORDER BY id DESC").fetchall():
            if row["task_id"] in known:
                continue
            assignments = [Assignment(recipient=item["recipient"], sender=item["sender"],
                status=item["status"], message=item["message"], finished_at=item["finished_at"])
                for item in conn.execute("SELECT * FROM send_log WHERE task_id=? ORDER BY id", (row["task_id"],))]
            done = row["success"] + row["failed"]
            result.append(TaskSummary(task_id=row["task_id"], subject=row["subject"], status="completed",
                total=row["total"], success=row["success"], failed=row["failed"], pending=max(0,row["total"]-done),
                progress=round(done/row["total"]*100,2) if row["total"] else 100,
                created_at=row["created_at"], updated_at=row["updated_at"], assignments=assignments))
        result.sort(key=lambda task: task.created_at, reverse=True)
        return result[:limit] if limit else result


def resolve_senders(sender_ids: list[int]) -> list[SenderConfig]:
    if not sender_ids or len(sender_ids) != len(set(sender_ids)):
        raise HTTPException(400, "请选择至少一个发送邮箱，不能重复选择")
    with db() as conn:
        rows = [conn.execute("SELECT * FROM sender_configs WHERE id=?", (sender_id,)).fetchone() for sender_id in sender_ids]
    if any(row is None or not row["enabled"] for row in rows):
        raise HTTPException(409, "所选发送邮箱不存在或已禁用，请刷新后重试")
    senders = [SenderConfig(id=str(row["id"]), email=row["email"], name=row["note"],
        auth_code=row["auth_code"], smtp_host=row["smtp_host"], smtp_port=row["smtp_port"]) for row in rows]
    addresses = [normalize_email(str(sender.email)) for sender in senders]
    if len(set(addresses)) != len(addresses):
        raise HTTPException(409, "同一发送邮箱存在多条配置，请只启用其中一条")
    return senders


def check_recipients(conn, addresses: list[str], exclude_task: str = "") -> None:
    enabled = {normalize_email(row["email"]) for row in conn.execute("SELECT email FROM recipients WHERE enabled=1")}
    invalid = [address for address in addresses if address not in enabled]
    if invalid:
        raise HTTPException(409, "收件人未保存或已禁用，请刷新列表：" + "、".join(invalid[:5]))
    claimed = set()
    for row in conn.execute("SELECT task_id,assignments FROM task_runs WHERE status!='completed'"):
        if row["task_id"] != exclude_task:
            claimed.update(item["recipient"] for item in json.loads(row["assignments"])
                           if item["status"] in {"pending", "sending", "uncertain"})
    if claimed.intersection(addresses):
        raise HTTPException(409, "部分收件人已在运行或中断的任务中，请先处理已有任务")


def persist_task(task: RuntimeTask, finished: Assignment | None = None) -> None:
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        if finished is not None:
            if finished.status == "success":
                matches = [(row["id"],) for row in conn.execute("SELECT id,email FROM recipients WHERE enabled=1")
                           if normalize_email(row["email"]) == finished.recipient]
                conn.executemany("UPDATE recipients SET enabled=0 WHERE id=?", matches)
            conn.execute("INSERT INTO send_log (task_id,recipient,sender,status,message,finished_at) VALUES (?,?,?,?,?,?)",
                (task.task_id, finished.recipient, finished.sender, finished.status, finished.message, finished.finished_at))
        conn.execute("UPDATE task_runs SET status=?,assignments=?,updated_at=? WHERE task_id=?",
            (task.status, json.dumps([item.model_dump() for item in task.assignments]), task.updated_at, task.task_id))
        conn.commit()


def schedule_task(task: RuntimeTask) -> None:
    TASKS[task.task_id] = task
    worker = asyncio.create_task(run_task(task.task_id))
    WORKERS[task.task_id] = worker
    def finished(done):
        WORKERS.pop(task.task_id, None)
        if not done.cancelled() and done.exception():
            logging.getLogger("mail_group").error("Task worker failed", exc_info=done.exception())
    worker.add_done_callback(finished)


def normalize_email(email: str) -> str:
    return str(clean_copied_email(email)).lower()


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
    pending = sum(1 for item in task.assignments if item.status in {"pending", "sending", "uncertain"})
    total = len(task.assignments)
    done = success + failed
    progress = round((done / total) * 100, 2) if total else 100.0
    return TaskSummary(
        task_id=task.task_id,
        subject=task.content.subject,
        uncertain=sum(item.status == "uncertain" for item in task.assignments),
        resumable=sum(item.status == "pending" for item in task.assignments),
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


def _translate_smtp_error(error: Exception) -> str:
    """将 SMTP 异常翻译为中文"""
    msg = str(error)
    mapping: dict[str, str] = {
        "Connection unexpectedly closed": "连接被服务器关闭（可能端口不通或被防火墙拦截，尝试换用 465 端口）",
        "timed out": "连接超时（网络不通或端口被防火墙拦截）",
        "Connection refused": "连接被拒绝（端口错误或服务未开启）",
        "authentication failed": "认证失败（授权码或密码错误）",
        "535": "认证失败（授权码或密码错误，请检查后重试）",
        "550": "邮件被拒收（收件人不存在或邮箱不可用）",
        "552": "邮件大小超出限制",
        "554": "邮件被拒收（可能被判定为垃圾邮件）",
        "421": "服务暂不可用，请稍后重试",
        "450": "邮箱暂时不可用",
        "451": "本地处理错误，请稍后重试",
        "452": "存储空间不足",
        "500": "SMTP 命令语法错误",
        "501": "SMTP 参数语法错误",
        "502": "SMTP 命令未实现",
        "503": "SMTP 命令顺序错误",
        "Network is unreachable": "网络不可达（请检查网络连接）",
        "Name or service not known": "DNS 解析失败（SMTP 地址错误）",
        "SSLEOFError": "SSL 连接失败（端口配置可能不正确，465 用 SSL，587 用 STARTTLS）",
        "[SSL: WRONG_VERSION_NUMBER]": "TLS/SSL 版本不匹配（请尝试切换端口 465/587）",
        "[SSL]": "SSL/TLS 连接失败（请尝试切换端口 465/587）",
    }
    for en, zh in mapping.items():
        if en.lower() in msg.lower():
            return zh
    # 提取 SMTP 错误码
    import re
    m = re.search(r"\((\d{3}),\s*b['\"](.+?)['\"]\s*\)", msg)
    if m:
        code, detail = m.group(1), m.group(2)
        return f"SMTP 错误 ({code}): {detail}"
    return msg


def _create_smtp_connection(host: str, port: int, timeout: int = 30) -> smtplib.SMTP:
    """根据端口自动选择 SSL 直连或 STARTTLS 连接"""
    context = ssl.create_default_context()
    if port == 465:
        return smtplib.SMTP_SSL(host, port, context=context, timeout=timeout)
    else:
        smtp = smtplib.SMTP(host, port, timeout=timeout)
        smtp.starttls(context=context)
        return smtp


def send_email(sender: SenderConfig, recipient: str, content: MailContent, attachments: list[MailAttachment]) -> None:
    message = EmailMessage()
    message["From"] = f"{sender.name} <{sender.email}>" if sender.name else str(sender.email)
    message["To"] = recipient
    message["Subject"] = content.subject
    message.set_content(content.body, subtype=content.content_type, charset="utf-8")

    for attachment in attachments:
        maintype, subtype = attachment.content_type.split("/", 1) if "/" in attachment.content_type else ("application", "octet-stream")
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )

    with _create_smtp_connection(sender.smtp_host, sender.smtp_port) as smtp:
        smtp.login(str(sender.email), sender.auth_code)
        smtp.send_message(message)


async def parse_attachments(files: list[UploadFile]) -> list[MailAttachment]:
    attachments: list[MailAttachment] = []
    for file in files:
        filename = file.filename or "attachment"
        if filename != filename.rstrip(" .") or filename != os.path.basename(filename) or "/" in filename or "\\" in filename or any(ord(c) < 32 for c in filename) or any(c in filename for c in ':*?"<>|'):
            raise HTTPException(400, "附件名称包含无效字符，请重命名后再试")
        lowered = filename.lower()
        if any(lowered.endswith(extension) for extension in BLOCKED_ATTACHMENT_EXTENSIONS):
            raise HTTPException(status_code=400, detail=f"{filename} 是 QQ 邮箱不建议发送的附件类型")
        content = await file.read(MAX_ATTACHMENT_SIZE + 1)
        if len(content) > MAX_ATTACHMENT_SIZE:
            raise HTTPException(status_code=400, detail=f"{filename} 超过 50MB 限制")
        content_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        attachments.append(MailAttachment(filename=filename, content=content, content_type=content_type))
    return attachments


async def run_task(task_id: str) -> None:
    task = TASKS[task_id]
    task.status = "running"
    persist_task(task)

    async def sender_group(assignments: list[Assignment]) -> None:
        for assignment in assignments:
            key = normalize_email(assignment.sender)
            lock = SENDER_LOCKS.setdefault(key, asyncio.Lock())
            async with lock:
                while not STOPPING and SENDER_NEXT_SEND.get(key, 0) > time.monotonic():
                    await asyncio.sleep(min(0.5, SENDER_NEXT_SEND[key] - time.monotonic()))
                if STOPPING:
                    return
                assignment.status = "sending"
                persist_task(task)  # Persist before the external side effect.
                try:
                    await asyncio.to_thread(send_email, task.senders[assignment.sender], assignment.recipient, task.content, task.attachments)
                    assignment.status = "success"
                    assignment.message = "发送成功"
                except asyncio.CancelledError:
                    assignment.status = "uncertain"
                    assignment.message = "发送被中断，请核对是否已送达"
                    raise
                except Exception as exc:
                    assignment.status = "failed"
                    assignment.message = _translate_smtp_error(exc)
                finally:
                    SENDER_NEXT_SEND[key] = time.monotonic() + random.uniform(*SEND_INTERVAL)
                assignment.finished_at = datetime.now().isoformat(timespec="seconds")
                persist_task(task, finished=assignment)

    try:
        groups: dict[str, list[Assignment]] = {}
        for assignment in task.assignments:
            if assignment.status == "pending":
                groups.setdefault(assignment.sender, []).append(assignment)
        await asyncio.gather(*(sender_group(group) for group in groups.values()))
        task.status = "interrupted" if any(a.status in {"pending", "sending", "uncertain"} for a in task.assignments) else "completed"
    except BaseException:
        task.status = "interrupted"
        for item in task.assignments:
            if item.status == "sending":
                item.status = "uncertain"
                item.message = "发送状态未确认，请核对后再重试"
        raise
    finally:
        persist_task(task)
        TASKS.pop(task_id, None)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tasks", response_model=TaskSummary)
async def create_task(payload: str = Form(...), attachments: list[UploadFile] = File(default=[])) -> TaskSummary:
    if STOPPING:
        raise HTTPException(503, "应用正在关闭，请稍后重新打开")
    try:
        parsed = SendTaskRequest.model_validate_json(payload)
    except ValidationError as exc:
        raise HTTPException(400, validation_details(exc)) from exc
    files = await parse_attachments(attachments)
    digest_data = parsed.model_dump(exclude={"request_id"})
    digest_data["attachments"] = [(f.filename, f.content_type, hashlib.sha256(f.content).hexdigest()) for f in files]
    digest = hashlib.sha256(json.dumps(digest_data, sort_keys=True).encode()).hexdigest()
    async with TASK_LOCK:
        with db() as conn:
            previous = conn.execute("SELECT * FROM task_runs WHERE request_id=?", (parsed.request_id,)).fetchone()
        if previous:
            if previous["request_digest"] != digest:
                raise HTTPException(409, "此请求编号已用于不同内容，请重新创建任务")
            return to_summary(TASKS[previous["task_id"]]) if previous["task_id"] in TASKS else summary_from_row(previous)
        senders = resolve_senders(parsed.sender_ids)
        assignments = build_assignments(senders, parsed.recipients)
        now = datetime.now().isoformat(timespec="seconds")
        task_id = uuid4().hex
        folder = os.path.join(DATA_DIR, "task_attachments", task_id)
        names = []
        try:
            if files:
                os.makedirs(folder, exist_ok=False)
                for attachment in files:
                    name = uuid4().hex + "_" + attachment.filename
                    with open(os.path.join(folder, name), "wb") as stream:
                        stream.write(attachment.content)
                    names.append(name)
            with db() as conn:
                check_recipients(conn, [a.recipient for a in assignments])
                conn.execute("INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (task_id, parsed.request_id, digest, parsed.model_dump_json(),
                     json.dumps([a.model_dump() for a in assignments]), json.dumps(names), "pending", now, now))
                conn.commit()
        except BaseException:
            # folder is a new UUID subdirectory created exclusively by this request.
            if os.path.isdir(folder):
                shutil.rmtree(folder)
            raise
        task = RuntimeTask(task_id, parsed.content, {str(s.email): s for s in senders}, assignments,
                           files, "pending", now, now, parsed.sender_ids, names)
        schedule_task(task)
        return to_summary(task)


@app.get("/api/tasks", response_model=list[TaskSummary])
async def list_tasks() -> list[TaskSummary]:
    return load_task_history()


@app.get("/api/tasks/{task_id}", response_model=TaskSummary)
async def get_task(task_id: str) -> TaskSummary:
    for summary in load_task_history():
        if summary.task_id == task_id:
            return summary
    raise HTTPException(404, "任务不存在")


@app.post("/api/tasks/{task_id}/resume", response_model=TaskSummary)
async def resume_task(task_id: str, payload: ResumeRequest) -> TaskSummary:
    if STOPPING:
        raise HTTPException(503, "应用正在关闭")
    async with TASK_LOCK:
        with db() as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "任务不存在或为不支持恢复的旧任务")
        if row["status"] != "interrupted" or task_id in WORKERS:
            raise HTTPException(409, "只有中断任务可以恢复")
        saved = SendTaskRequest.model_validate_json(row["request_json"])
        senders = resolve_senders(saved.sender_ids)
        assignments = [Assignment(**item) for item in json.loads(row["assignments"])]
        for item in assignments:
            if payload.retry_uncertain and item.status == "uncertain":
                item.status = "pending"
                item.message = "已确认需要重新发送"
        pending = [a for a in assignments if a.status == "pending"]
        if not pending:
            raise HTTPException(409, "没有可恢复的邮件；未确认状态需核对后再选择重试")
        # Configuration may have changed since interruption; redistribute only unsent members.
        cycle_senders = cycle(senders)
        for item in pending:
            item.sender = str(next(cycle_senders).email)
        names = json.loads(row["attachment_names"])
        files = []
        for name in names:
            if os.path.basename(name) != name:
                raise HTTPException(409, "任务附件路径无效")
            path = os.path.join(DATA_DIR, "task_attachments", task_id, name)
            if not os.path.isfile(path):
                raise HTTPException(409, "任务附件已丢失，无法恢复；请重新创建邮件")
            with open(path, "rb") as stream:
                files.append(MailAttachment(name.split("_", 1)[-1], stream.read(), mimetypes.guess_type(name)[0] or "application/octet-stream"))
        with db() as conn:
            check_recipients(conn, [a.recipient for a in pending], exclude_task=task_id)
        task = RuntimeTask(task_id, saved.content, {str(s.email): s for s in senders}, assignments,
                           files, "pending", row["created_at"], row["updated_at"], saved.sender_ids, names)
        persist_task(task)
        schedule_task(task)
        return to_summary(task)


@app.post("/api/assignments/preview", response_model=list[Assignment])
def preview_assignments(payload: SendTaskRequest) -> list[Assignment]:
    return build_assignments(resolve_senders(payload.sender_ids), payload.recipients)


@app.post("/api/shutdown")
async def shutdown_service():
    global STOPPING
    STOPPING = True
    async def drain():
        if WORKERS:
            await asyncio.gather(*list(WORKERS.values()), return_exceptions=True)
        if SERVER is not None:
            SERVER.should_exit = True
    asyncio.create_task(drain())
    return {"status": "stopping"}


# ── 保存邮件 ──────────────────────────────────────────────────────────────


@app.post("/api/saved-mails", response_model=SavedMailOut)
async def save_mail(
    subject: str = Form(..., min_length=1, max_length=200),
    body: str = Form(..., min_length=1),
    content_type: Literal["plain", "html"] = Form("plain"),
    attachments: list[UploadFile] = File(default=[]),
) -> SavedMailOut:
    now = datetime.now().isoformat(timespec="seconds")
    parsed = await parse_attachments(attachments)
    names = []
    os.makedirs(SAVED_ATTACHMENTS_DIR, exist_ok=True)
    try:
        for attachment in parsed:
            name = f"{uuid4().hex}_{attachment.filename}"
            names.append(name)
            with open(os.path.join(SAVED_ATTACHMENTS_DIR, name), "wb") as stream:
                stream.write(attachment.content)
        with db() as conn:
            cursor = conn.execute("INSERT INTO saved_mails (subject,body,content_type,created_at,attachments) VALUES (?,?,?,?,?)",
                (subject, body, content_type, now, json.dumps(names)))
            conn.commit()
            return SavedMailOut(id=cursor.lastrowid, subject=subject, body=body, content_type=content_type, created_at=now, attachments=names)
    except BaseException:
        for name in names:
            path = os.path.join(SAVED_ATTACHMENTS_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        raise


@app.get("/api/saved-mails", response_model=list[SavedMailOut])
def list_saved_mails() -> list[SavedMailOut]:
    with db() as conn:
        rows = conn.execute("SELECT id, subject, body, content_type, created_at, attachments FROM saved_mails ORDER BY id DESC").fetchall()
        result: list[SavedMailOut] = []
        for row in rows:
            atts = json.loads(row["attachments"]) if row["attachments"] else []
            result.append(SavedMailOut(id=row["id"], subject=row["subject"], body=row["body"], content_type=row["content_type"], created_at=row["created_at"], attachments=atts))
        return result


@app.get("/api/saved-mails/{mail_id}", response_model=SavedMailOut)
def get_saved_mail(mail_id: int) -> SavedMailOut:
    with db() as conn:
        row = conn.execute("SELECT id, subject, body, content_type, created_at, attachments FROM saved_mails WHERE id = ?", (mail_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="保存的邮件不存在")
        atts = json.loads(row["attachments"]) if row["attachments"] else []
        return SavedMailOut(id=row["id"], subject=row["subject"], body=row["body"], content_type=row["content_type"], created_at=row["created_at"], attachments=atts)


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
def update_sender_config(sender_id: int, payload: StoredSenderUpdate) -> StoredSender:
    with db() as conn:
        row = conn.execute("SELECT id, enabled, created_at, auth_code FROM sender_configs WHERE id = ?", (sender_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="发送邮箱不存在")
        conn.execute(
            "UPDATE sender_configs SET email=?, auth_code=?, smtp_host=?, smtp_port=?, note=? WHERE id=?",
            (payload.email, payload.auth_code or row["auth_code"], payload.smtp_host, payload.smtp_port, payload.note, sender_id),
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
        try:
            conn.execute("UPDATE recipients SET email=?, note=? WHERE id=?", (new_email, new_note, recipient_id))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该邮箱已存在") from exc
        conn.commit()
        return StoredRecipient(id=row["id"], email=new_email, enabled=bool(row["enabled"]), note=new_note, created_at=row["created_at"])


# ── 统计数据 ──────────────────────────────────────────────────────────────


def _period_range(period: str) -> tuple[datetime, datetime, datetime]:
    now = datetime.now()
    if period == "month":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, start.replace(year=start.year + 1), start.replace(year=start.year - 1)
    if period in {"day", "week"}:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        previous = (start - timedelta(days=1)).replace(day=1)
        return start, end, previous
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1), start - timedelta(days=1)


def _previous_period_range(period: str) -> tuple[str, str]:
    start, _, previous = _period_range(period)
    return previous.isoformat(timespec="seconds"), start.isoformat(timespec="seconds")


def _count_period(conn: sqlite3.Connection, start: str, end: str) -> tuple[int, int]:
    row = conn.execute("SELECT SUM(status='success') AS s, SUM(status='failed') AS f FROM send_log WHERE finished_at>=? AND finished_at<?", (start, end)).fetchone()
    return row["s"] or 0, row["f"] or 0


@app.get("/api/stats", response_model=StatsResponse)
def get_stats(period: str = "hour") -> StatsResponse:
    if period not in {"hour", "day", "week", "month"}:
        period = "hour"
    start, end, previous = _period_range(period)
    labels = {
        "hour": "strftime('%Y-%m-%d %H:00', finished_at)",
        "day": "strftime('%Y-%m-%d', finished_at)",
        "week": "strftime('%Y-%m', finished_at) || '-' || CAST((CAST(strftime('%d', finished_at) AS INTEGER)-1)/7+1 AS INTEGER)",
        "month": "strftime('%Y-%m', finished_at)",
    }
    with db() as conn:
        rows = conn.execute(f"SELECT {labels[period]} AS label, COUNT(*) AS total, SUM(status='success') AS success, SUM(status='failed') AS failed FROM send_log WHERE finished_at>=? AND finished_at<? GROUP BY label ORDER BY label", (start.isoformat(), end.isoformat())).fetchall()
        total = conn.execute("SELECT SUM(status='success') AS s, SUM(status='failed') AS f FROM send_log").fetchone()
        total_s, total_f = total["s"] or 0, total["f"] or 0
        prev_s, prev_f = _count_period(conn, previous.isoformat(), start.isoformat())
        period_s = sum(row["success"] for row in rows)
        period_f = sum(row["failed"] for row in rows)
    return StatsResponse(stats=[StatItem(**dict(row)) for row in rows], total_success=total_s, total_failed=total_f,
        success_rate=round(total_s/(total_s+total_f)*100,2) if total_s+total_f else 0,
        success_prev=prev_s, failed_prev=prev_f, total_prev=prev_s+prev_f,
        rate_prev=round(prev_s/(prev_s+prev_f)*100,2) if prev_s+prev_f else 0,
        period_success=period_s, period_failed=period_f,
        period_rate=round(period_s/(period_s+period_f)*100,2) if period_s+period_f else 0)


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


# ── 邮箱类型预设 ──────────────────────────────────────────────────────────

EMAIL_PRESETS = [
    {
        "type": "qq",
        "label": "QQ 邮箱",
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "desc": "使用 SMTP 授权码登录",
    },
    {
        "type": "126",
        "label": "126 邮箱",
        "smtp_host": "smtp.126.com",
        "smtp_port": 465,
        "desc": "使用 SMTP 授权码登录（如 465 不通可尝试 587）",
    },
    {
        "type": "163",
        "label": "163 邮箱",
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "desc": "使用 SMTP 授权码登录（如 465 不通可尝试 587）",
    },
    {
        "type": "gmail",
        "label": "Gmail",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "desc": "需开启两步验证并生成应用专用密码",
    },
    {
        "type": "outlook",
        "label": "Outlook / Hotmail",
        "smtp_host": "smtp-mail.outlook.com",
        "smtp_port": 587,
        "desc": "使用邮箱密码登录",
    },
    {
        "type": "sina",
        "label": "新浪邮箱",
        "smtp_host": "smtp.sina.com",
        "smtp_port": 587,
        "desc": "使用 SMTP 授权码或邮箱密码",
    },
    {
        "type": "sohu",
        "label": "搜狐邮箱",
        "smtp_host": "smtp.sohu.com",
        "smtp_port": 465,
        "desc": "使用邮箱密码登录",
    },
    {
        "type": "aliyun",
        "label": "阿里企业邮箱",
        "smtp_host": "smtp.qiye.aliyun.com",
        "smtp_port": 465,
        "desc": "使用邮箱密码登录",
    },
    {
        "type": "other",
        "label": "其它邮箱",
        "smtp_host": "",
        "smtp_port": 465,
        "desc": "自行填写 SMTP 服务器地址和端口",
    },
]


@app.get("/api/email-presets")
def get_email_presets() -> list[dict]:
    return EMAIL_PRESETS


# ── 静态文件（前端） ──────────────────────────────────────────────────────

frontend_path = _frontend_dir()
if frontend_path:
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")


# ── 入口 ──────────────────────────────────────────────────────────────────

def run_server():
    global SERVER
    import argparse
    import socket
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_file = open(os.path.join(DATA_DIR, "service.lock"), "a+b")
    if os.name == "nt":
        import msvcrt
        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.write(b"0"); lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("数据目录正在被另一实例使用，请先关闭已运行的应用")
    logging.basicConfig(filename=os.path.join(DATA_DIR, "server.log"), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(128)
    port = listener.getsockname()[1]
    SERVER = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    if args.parent_pid and os.name == "nt":
        def watch_parent():
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, args.parent_pid)
            if handle:
                kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
                kernel.CloseHandle(handle)
            SERVER.should_exit = True
        threading.Thread(target=watch_parent, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    if args.desktop:
        print(json.dumps({"event": "ready", "port": port}), flush=True)
    else:
        def open_browser():
            time.sleep(1)
            webbrowser.open(url + "/#session=" + API_TOKEN)
        threading.Thread(target=open_browser, daemon=True).start()
        print("邮件群发助手已启动，浏览器将自动打开。", flush=True)
    try:
        SERVER.run(sockets=[listener])
    finally:
        listener.close()
        lock_file.close()


if __name__ == "__main__":
    run_server()
