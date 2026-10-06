"""Read-only IMAP aggregation. Credentials and message content stay on this machine."""
from __future__ import annotations

import base64
import imaplib
import json
import re
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Literal

from fastapi import HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator


IMAP_HOSTS = {
    'smtp.qq.com': 'imap.qq.com', 'smtp.exmail.qq.com': 'imap.exmail.qq.com',
    'smtp.163.com': 'imap.163.com', 'smtp.126.com': 'imap.126.com',
    'smtp.gmail.com': 'imap.gmail.com', 'smtp-mail.outlook.com': 'outlook.office365.com',
    'smtp.office365.com': 'outlook.office365.com', 'smtp.sina.com': 'imap.sina.com',
    'smtp.sohu.com': 'imap.sohu.com', 'smtp.qiye.aliyun.com': 'imap.qiye.aliyun.com',
}
BATCH_SIZE = 100
BODY_LIMIT = 10 * 1024 * 1024
TEXT_LIMIT = 500_000
HEADER_FIELDS = '(SUBJECT FROM TO CC DATE MESSAGE-ID)'


class MailboxSettings(BaseModel):
    imap_host: str = Field(default='', max_length=253)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_security: Literal['ssl', 'starttls'] = 'ssl'
    imap_sent_folder: str = Field(default='', max_length=255)

    @field_validator('imap_host')
    @classmethod
    def valid_host(cls, value):
        value = value.strip().lower()
        if value and not re.fullmatch(r'[a-z0-9][a-z0-9.\-]*', value):
            raise ValueError('IMAP 地址只需填写服务器域名，不要包含协议、路径或端口')
        return value

    @field_validator('imap_sent_folder')
    @classmethod
    def valid_folder(cls, value):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('文件夹名称不能含控制字符')
        return value.strip()


def init_schema(conn):
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(sender_configs)')}
    additions = {
        'imap_host': "TEXT NOT NULL DEFAULT ''", 'imap_port': 'INTEGER NOT NULL DEFAULT 993',
        'imap_security': "TEXT NOT NULL DEFAULT 'ssl'", 'imap_sent_folder': "TEXT NOT NULL DEFAULT ''",
        'mailbox_revision': 'INTEGER NOT NULL DEFAULT 1',
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE sender_configs ADD COLUMN {name} {definition}')
    conn.execute('''CREATE TABLE IF NOT EXISTS mailbox_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL,
        folder_kind TEXT NOT NULL, folder_name TEXT NOT NULL, uidvalidity TEXT NOT NULL,
        uid INTEGER NOT NULL, subject TEXT NOT NULL, from_text TEXT NOT NULL, to_text TEXT NOT NULL,
        cc_text TEXT NOT NULL, message_date TEXT NOT NULL, message_id TEXT NOT NULL,
        unread INTEGER NOT NULL, size INTEGER NOT NULL, body TEXT,
        attachments TEXT NOT NULL DEFAULT '[]', truncated INTEGER NOT NULL DEFAULT 0,
        UNIQUE(sender_id, folder_name, uidvalidity, uid))''')
    conn.execute('CREATE INDEX IF NOT EXISTS mailbox_date ON mailbox_messages(message_date DESC, id DESC)')
    if 'source' not in {r['name'] for r in conn.execute('PRAGMA table_info(mailbox_messages)')}:
        conn.execute("ALTER TABLE mailbox_messages ADD COLUMN source TEXT NOT NULL DEFAULT 'imap'")
    conn.execute('CREATE INDEX IF NOT EXISTS mailbox_identity ON mailbox_messages(sender_id,source,message_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS mailbox_folders (
        sender_id INTEGER NOT NULL, folder_kind TEXT NOT NULL, folder_name TEXT NOT NULL,
        uidvalidity TEXT NOT NULL, remaining INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(sender_id, folder_kind))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS mailbox_sync_state (
        sender_id INTEGER PRIMARY KEY, synced_at TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '')''')


def clear_account(conn, sender_id, keep_local=False):
    for table in ('mailbox_messages', 'mailbox_folders', 'mailbox_sync_state'):
        suffix = " AND source='imap'" if table == 'mailbox_messages' and keep_local else ''
        conn.execute(f'DELETE FROM {table} WHERE sender_id=?{suffix}', (sender_id,))


def save_local_sent(conn, sender_id, task_id, index, sender, recipient, content, filenames, finished_at, message_id=''):
    account = conn.execute('SELECT email FROM sender_configs WHERE id=?', (sender_id,)).fetchone()
    if not account or account['email'].lower() != sender.lower():
        return
    body = content['body']
    if content.get('content_type') == 'html':
        parser = PlainHTML()
        parser.feed(body)
        body = ''.join(parser.parts)
    date = datetime.fromisoformat(finished_at).astimezone(timezone.utc).isoformat(timespec='seconds')
    conn.execute('''INSERT OR IGNORE INTO mailbox_messages
        (sender_id,folder_kind,folder_name,uidvalidity,uid,subject,from_text,to_text,cc_text,message_date,message_id,unread,size,body,attachments,truncated,source)
        VALUES (?,'sent',?,'local',?,?,?,?, '',?,?,0,0,?,?,?,'local')''',
        (sender_id, '@local/' + task_id, index + 1, content['subject'], sender, recipient, date, message_id,
            body[:TEXT_LIMIT], json.dumps(filenames, ensure_ascii=False), int(len(body) > TEXT_LIMIT)))


def migrate_local_sent(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS mailbox_migrations (name TEXT PRIMARY KEY)')
    if conn.execute("SELECT 1 FROM mailbox_migrations WHERE name='local-history'").fetchone():
        return
    accounts = {r['email'].lower(): r['id'] for r in conn.execute('SELECT id,email FROM sender_configs')}
    for task in conn.execute('SELECT * FROM task_runs').fetchall():
        request = json.loads(task['request_json'])
        templates = {t['template_id']: t for t in json.loads(task['template_snapshots'])}
        for index, item in enumerate(json.loads(task['assignments'])):
            sender_id = accounts.get(item['sender'].lower())
            if not sender_id or item['status'] != 'success' or not item.get('finished_at'):
                continue
            template = templates.get(item.get('template_id'))
            content = template['content'] if template else request.get('content')
            if content:
                names = [a['name'] for a in template['attachments']] if template else json.loads(task['attachment_names'])
                save_local_sent(conn, sender_id, task['task_id'], index, item['sender'], item['recipient'], content,
                    [name.split('_', 1)[-1] for name in names], item['finished_at'], item.get('message_id', ''))
    conn.execute("INSERT INTO mailbox_migrations VALUES ('local-history')")


VISIBLE = """NOT (m.source='local' AND m.message_id!='' AND EXISTS
    (SELECT 1 FROM mailbox_messages remote WHERE remote.sender_id=m.sender_id AND remote.source='imap'
        AND remote.folder_kind='sent' AND remote.message_id=m.message_id))"""


def encode_folder(value):
    """IMAP modified UTF-7, including literal ampersands and localized folder names."""
    result, pending = [], []
    def flush():
        if pending:
            encoded = base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/', ',')
            result.append('&' + encoded + '-')
            pending.clear()
    for char in value:
        if 32 <= ord(char) <= 126:
            flush()
            result.append('&-' if char == '&' else char)
        else:
            pending.append(char)
    flush()
    return ''.join(result)


def decode_folder(value):
    def decode(match):
        encoded = match.group(1)
        if not encoded:
            return '&'
        try:
            return base64.b64decode(encoded.replace(',', '/') + '=' * (-len(encoded) % 4)).decode('utf-16-be')
        except (ValueError, UnicodeError):
            return match.group(0)
    return re.sub(r'&([^-]*)-', decode, value)


def quote_folder(value):
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise MailboxError('邮箱返回的文件夹名称无效')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


class MailboxError(Exception):
    """Only application-authored messages may reach the UI."""


def safe_error(error):
    if isinstance(error, MailboxError):
        return str(error)
    if isinstance(error, ssl.SSLError):
        return 'IMAP 加密连接或证书校验失败，请检查服务器地址与加密方式'
    if isinstance(error, (TimeoutError, OSError)):
        return 'IMAP 连接失败或超时，请检查网络、服务器地址和端口后重试'
    # A server error can contain credentials or private message content. Never return it verbatim.
    return 'IMAP 同步失败，请检查授权码及邮箱中的 IMAP 开关；要求 OAuth 登录的账号暂不支持'


@contextmanager
def connect(account):
    host = account['imap_host'] or IMAP_HOSTS.get(account['smtp_host'].lower(), '')
    if not host:
        raise MailboxError('请先在“邮箱集群”中为该账号填写 IMAP 服务器地址')
    context = ssl.create_default_context()
    client = None
    try:
        if account['imap_security'] == 'ssl':
            client = imaplib.IMAP4_SSL(host, account['imap_port'], ssl_context=context, timeout=10)
        else:
            client = imaplib.IMAP4(host, account['imap_port'], timeout=10)
            if client.starttls(ssl_context=context)[0] != 'OK':
                raise MailboxError('IMAP 服务器不支持所选的 STARTTLS 加密方式')
        if client.login(account['email'], account['auth_code'])[0] != 'OK':
            raise MailboxError('IMAP 登录失败，请检查授权码和 IMAP 开关')
        # NetEase requires RFC 2971 client identification before SELECT on some accounts.
        if b'ID' in client.capabilities or 'ID' in client.capabilities:
            imaplib.Commands.setdefault('ID', ('AUTH', 'SELECTED'))
            client._simple_command('ID', '("name" "MailGroup" "version" "3.2")')
        yield client
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                try:
                    client.shutdown()
                except Exception:
                    pass


def sent_folder(client, custom):
    if custom:
        return encode_folder(custom)
    status, lines = client.list()
    if status != 'OK':
        raise MailboxError('无法读取文件夹列表，请在账号设置中指定已发送文件夹')
    fallback = None
    for item in lines or []:
        if isinstance(item, tuple):
            prefix, literal = item
            text = prefix.decode('ascii', 'replace')
            match = re.match(r'^\(([^)]*)\)\s+(?:"(?:\\.|[^"\\])*"|NIL)\s+', text)
            name = literal.decode('ascii', 'replace')
        elif isinstance(item, bytes):
            text = item.decode('ascii', 'replace')
            match = re.match(r'^\(([^)]*)\)\s+(?:"(?:\\.|[^"\\])*"|NIL)\s+(.+)$', text)
            if not match:
                continue
            name = match.group(2)
            if name.startswith('"') and name.endswith('"'):
                name = re.sub(r'\\(.)', r'\1', name[1:-1])
        else:
            continue
        if not match or '\\noselect' in match.group(1).lower():
            continue
        if '\\sent' in match.group(1).lower().split():
            return name
        leaf = re.split(r'[/\\.]', decode_folder(name).lower())[-1]
        if leaf in {'sent', 'sent messages', 'sent items', 'sent mail', '已发送', '已发送邮件', '已发邮件'}:
            fallback = name
    return fallback


def select_folder(client, name):
    status, _ = client.select(quote_folder(name), readonly=True)
    if status != 'OK':
        raise MailboxError('无法打开邮件文件夹，请检查 IMAP 权限或已发送文件夹设置')
    _, values = client.response('UIDVALIDITY')
    validity = values[0].decode('ascii') if values and isinstance(values[0], bytes) else ''
    if not validity.isdigit() or not 0 < int(validity) < 2**32:
        raise MailboxError('服务器未提供有效的邮件标识，请稍后重试')
    return validity


def message_date(header, envelope):
    internal = re.search(rb'INTERNALDATE "([^"]+)"', envelope)
    for value in (str(header.get('Date', '')), internal.group(1).decode('ascii', 'replace') if internal else ''):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return date.astimezone(timezone.utc).isoformat(timespec='seconds')
        except (ValueError, TypeError, OverflowError):
            continue
    return '1970-01-01T00:00:00+00:00'


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'head', 'noscript'}:
            self.hidden += 1
        if not self.hidden and tag in {'br', 'p', 'div', 'tr', 'li', 'h1', 'h2', 'h3'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'head', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {'p', 'div', 'tr', 'li'}:
            self.parts.append('\n')

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def parse_body(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    part = message.get_body(preferencelist=('plain', 'html'))
    body = ''
    if part:
        try:
            body = part.get_content()
        except (LookupError, UnicodeError):
            body = (part.get_payload(decode=True) or b'').decode('utf-8', 'replace')
        if part.get_content_type() == 'text/html':
            parser = PlainHTML()
            parser.feed(body)
            body = ''.join(parser.parts)
    names = [str(part.get_filename())[:500] for part in message.walk() if part.get_filename()]
    return body[:TEXT_LIMIT].strip(), names[:100], len(body) > TEXT_LIMIT


class MailboxService:
    def __init__(self, db):
        self.db = db
        self._locks = {}
        self._lock = threading.Lock()

    def account(self, sender_id):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM sender_configs WHERE id=?', (sender_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '发送邮箱不存在')
        return dict(row)

    def guard(self, conn, account):
        row = conn.execute('SELECT mailbox_revision FROM sender_configs WHERE id=?', (account['id'],)).fetchone()
        if row is None or row['mailbox_revision'] != account['mailbox_revision']:
            raise HTTPException(409, '邮箱配置已更改，请重新同步')

    @contextmanager
    def account_lock(self, sender_id):
        with self._lock:
            lock = self._locks.setdefault(sender_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '该邮箱正在同步或读取邮件，请稍后重试')
        try:
            yield
        finally:
            lock.release()

    def accounts(self):
        with self.db() as conn:
            rows = conn.execute(f'''SELECT s.id, s.email, s.note, s.imap_host, s.smtp_host,
                COALESCE(st.synced_at, '') AS synced_at, COALESCE(st.last_error, '') AS last_error,
                (SELECT COUNT(*) FROM mailbox_messages m WHERE m.sender_id=s.id AND {VISIBLE}) AS cached,
                COALESCE((SELECT SUM(remaining) FROM mailbox_folders f WHERE f.sender_id=s.id), 0) AS remaining
                FROM sender_configs s LEFT JOIN mailbox_sync_state st ON st.sender_id=s.id ORDER BY s.id DESC''').fetchall()
        return [dict(id=r['id'], email=r['email'], note=r['note'], configured=bool(r['imap_host'] or IMAP_HOSTS.get(r['smtp_host'].lower())),
            synced_at=r['synced_at'], last_error=r['last_error'], cached=r['cached'], remaining=r['remaining']) for r in rows]

    def sync_folder(self, client, account, kind, name, deadline):
        if time.monotonic() > deadline:
            raise MailboxError('本次同步已达到时限，请继续同步')
        validity = select_folder(client, name)
        if time.monotonic() > deadline:
            raise MailboxError('本次同步已达到时限，请继续同步')
        status, data = client.uid('search', None, 'ALL')
        if status != 'OK' or not data or not isinstance(data[0], bytes):
            raise MailboxError('无法读取邮件列表，请检查邮箱权限')
        raw_uids = data[0].split() if data and isinstance(data[0], bytes) else []
        if any(not uid.isdigit() or not 0 < int(uid) < 2**32 for uid in raw_uids):
            raise MailboxError('服务器返回了无效的邮件列表')
        uids = {int(uid) for uid in raw_uids}
        with self.db() as conn:
            self.guard(conn, account)
            conn.execute("DELETE FROM mailbox_messages WHERE source='imap' AND sender_id=? AND folder_kind=? AND (folder_name!=? OR uidvalidity!=?)", (account['id'], kind, name, validity))
            existing = {r['uid'] for r in conn.execute("SELECT uid FROM mailbox_messages WHERE source='imap' AND sender_id=? AND folder_kind=?", (account['id'], kind))}
            # Reconcile server deletions without ever issuing a remote STORE or EXPUNGE.
            conn.executemany("DELETE FROM mailbox_messages WHERE source='imap' AND sender_id=? AND folder_kind=? AND uid=?", [(account['id'], kind, uid) for uid in existing - uids])
            conn.commit()
        missing = sorted(uids - existing, reverse=True)[:BATCH_SIZE]
        refresh = sorted(uids & existing, reverse=True)[:25]
        requested = missing + refresh
        received = set()
        for start in range(0, len(requested), 25):
            if time.monotonic() > deadline:
                raise MailboxError('本次同步已达到时限，已获取的邮件已保留，请继续同步')
            subset = set(requested[start:start + 25])
            status, data = client.uid('fetch', ','.join(map(str, sorted(subset))), f'(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS {HEADER_FIELDS}])')
            if status != 'OK':
                raise MailboxError('读取邮件摘要失败，已获取的邮件已保留，请重试')
            records = []
            for item in data or []:
                if not isinstance(item, tuple) or not isinstance(item[1], bytes):
                    continue
                envelope, raw = item
                uid_match = re.search(rb'\bUID (\d+)\b', envelope)
                if not uid_match or int(uid_match.group(1)) not in subset:
                    continue
                uid = int(uid_match.group(1))
                if len(raw) > 256_000:
                    raise MailboxError('邮件头过大，无法同步该批次')
                header = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
                size = re.search(rb'RFC822.SIZE (\d+)', envelope)
                records.append((account['id'], kind, name, validity, uid,
                    str(header.get('Subject', '（无主题）'))[:2000], str(header.get('From', ''))[:4000],
                    str(header.get('To', ''))[:8000], str(header.get('Cc', ''))[:8000], message_date(header, envelope),
                    str(header.get('Message-ID', ''))[:1000], int(b'\\Seen' not in imaplib.ParseFlags(envelope)), int(size.group(1)) if size else 0))
                received.add(uid)
            with self.db() as conn:
                self.guard(conn, account)
                conn.executemany('''INSERT INTO mailbox_messages
                    (sender_id,folder_kind,folder_name,uidvalidity,uid,subject,from_text,to_text,cc_text,message_date,message_id,unread,size)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sender_id,folder_name,uidvalidity,uid)
                    DO UPDATE SET unread=excluded.unread,subject=excluded.subject,from_text=excluded.from_text,to_text=excluded.to_text''', records)
                conn.commit()
        remaining = len(uids - ((existing & uids) | received))
        with self.db() as conn:
            self.guard(conn, account)
            conn.execute('''INSERT INTO mailbox_folders VALUES (?,?,?,?,?) ON CONFLICT(sender_id,folder_kind)
                DO UPDATE SET folder_name=excluded.folder_name,uidvalidity=excluded.uidvalidity,remaining=excluded.remaining''', (account['id'], kind, name, validity, remaining))
            conn.commit()
        return len(received - existing), remaining

    def sync(self, sender_id):
        with self.account_lock(sender_id):
            account = self.account(sender_id)
            errors, count, remaining = [], 0, 0
            synced_at = ''
            try:
                deadline = time.monotonic() + 35
                with connect(account) as client:
                    folders = [('inbox', 'INBOX')]
                    try:
                        sent = sent_folder(client, account['imap_sent_folder'])
                        if sent and sent.upper() != 'INBOX':
                            folders.append(('sent', sent))
                        else:
                            errors.append('未识别到已发送文件夹，请在“邮箱集群”中指定；收件箱仍可同步')
                    except Exception as exc:
                        errors.append(safe_error(exc))
                    for kind, folder in folders:
                        try:
                            added, rest = self.sync_folder(client, account, kind, folder, deadline)
                            count += added
                            remaining += rest
                            synced_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
                        except HTTPException:
                            raise
                        except Exception as exc:
                            errors.append(('收件箱：' if kind == 'inbox' else '已发送：') + safe_error(exc))
            except HTTPException:
                raise
            except Exception as exc:
                errors.append(safe_error(exc))
            error = '；'.join(errors)
            with self.db() as conn:
                self.guard(conn, account)
                conn.execute('''INSERT INTO mailbox_sync_state VALUES (?,?,?) ON CONFLICT(sender_id)
                    DO UPDATE SET synced_at=CASE WHEN excluded.synced_at!='' THEN excluded.synced_at ELSE synced_at END,
                    last_error=excluded.last_error''', (sender_id, synced_at, error))
                conn.commit()
            return dict(sender_id=sender_id, added=count, remaining=remaining, error=error)

    def messages(self, sender_id=None, kind='all', query='', page=1, page_size=30):
        clauses, args = [VISIBLE], []
        if sender_id is not None:
            clauses.append('m.sender_id=?')
            args.append(sender_id)
        if kind != 'all':
            clauses.append('m.folder_kind=?')
            args.append(kind)
        if query.strip():
            clauses.append('(instr(lower(m.subject),lower(?))>0 OR instr(lower(m.from_text),lower(?))>0 OR instr(lower(m.to_text),lower(?))>0)')
            args.extend([query.strip()] * 3)
        where = ' AND '.join(clauses)
        with self.db() as conn:
            total = conn.execute(f'SELECT COUNT(*) FROM mailbox_messages m JOIN sender_configs s ON m.sender_id=s.id WHERE {where}', args).fetchone()[0]
            rows = conn.execute(f'''SELECT m.id,m.sender_id,s.email AS account_email,m.folder_kind,m.subject,m.from_text,
                m.to_text,m.message_date,m.unread,m.size,m.source,m.body IS NOT NULL AS body_cached
                FROM mailbox_messages m JOIN sender_configs s ON m.sender_id=s.id WHERE {where}
                ORDER BY m.message_date DESC,m.id DESC LIMIT ? OFFSET ?''', [*args, page_size, (page-1)*page_size]).fetchall()
        return dict(items=[dict(row) for row in rows], total=total, page=page, page_size=page_size)

    def detail(self, message_id):
        with self.db() as conn:
            row = conn.execute('SELECT * FROM mailbox_messages WHERE id=?', (message_id,)).fetchone()
            account_row = conn.execute('SELECT * FROM sender_configs WHERE id=?', (row['sender_id'],)).fetchone() if row else None
        if not row or not account_row:
            raise HTTPException(404, '邮件不存在或已从服务器移除，请刷新列表')
        result = dict(row)
        account = dict(account_row)
        if row['body'] is None:
            with self.account_lock(account['id']):
                try:
                    with connect(account) as client:
                        if select_folder(client, row['folder_name']) != row['uidvalidity']:
                            raise HTTPException(409, '邮箱邮件标识已变更，请重新同步后查看')
                        status, data = client.uid('fetch', str(row['uid']), f'(UID BODY.PEEK[]<0.{BODY_LIMIT}>)')
                        payload = next((item[1] for item in (data or []) if isinstance(item, tuple)
                            and re.search(rb'\bUID ' + str(row['uid']).encode() + rb'\b', item[0])), None)
                        if status != 'OK' or not isinstance(payload, bytes):
                            raise HTTPException(404, '邮件已移走或删除，请重新同步')
                        if len(payload) > BODY_LIMIT:
                            payload = payload[:BODY_LIMIT]
                        body, attachments, long_text = parse_body(payload)
                        truncated = long_text or row['size'] > BODY_LIMIT or len(payload) == BODY_LIMIT
                    with self.db() as conn:
                        self.guard(conn, account)
                        conn.execute('UPDATE mailbox_messages SET body=?,attachments=?,truncated=? WHERE id=?', (body, json.dumps(attachments, ensure_ascii=False), int(truncated), message_id))
                        conn.commit()
                    result.update(body=body, attachments=json.dumps(attachments), truncated=truncated)
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(502, safe_error(exc)) from None
        with self.db() as conn:
            self.guard(conn, account)
            if not conn.execute('SELECT id FROM mailbox_messages WHERE id=?', (message_id,)).fetchone():
                raise HTTPException(409, '邮件缓存已变更，请重新同步后查看')
        result['account_email'] = account['email']
        result['attachments'] = json.loads(result['attachments'])
        return result


def register_routes(app, service):
    @app.get('/api/mailbox/accounts')
    def accounts():
        return service.accounts()

    @app.post('/api/mailbox/sync/{sender_id}')
    def sync(sender_id: int = Path(ge=1, le=2**63 - 1)):
        return service.sync(sender_id)

    @app.get('/api/mailbox/messages')
    def messages(sender_id: int | None = Query(default=None, ge=1, le=2**63 - 1), kind: Literal['all', 'inbox', 'sent'] = 'all',
                 query: str = Query(default='', max_length=200), page: int = Query(default=1, ge=1, le=1000000),
                 page_size: int = Query(default=30, ge=1, le=100)):
        return service.messages(sender_id, kind, query, page, page_size)

    @app.get('/api/mailbox/messages/{message_id}')
    def detail(message_id: int = Path(ge=1, le=2**63 - 1)):
        return service.detail(message_id)
