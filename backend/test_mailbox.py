"""All mail and credentials here are generated fixtures; no external email server is used."""
import json
import ssl
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

import main
import mailbox_service as mb


def raw_mail(subject='项目进度', html=False, attachment=False):
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = '测试客户 <customer@example.com>'
    message['To'] = 'team@example.com'
    message['Date'] = 'Tue, 06 Oct 2026 09:30:00 +0800'
    message['Message-ID'] = '<synthetic@example.com>'
    if html:
        message.set_content('<html><head><style>hidden</style></head><body><p>你好 &amp; world</p><img src="https://example.com/tracker"><script>bad()</script></body></html>', subtype='html')
    else:
        message.set_content('你好，项目进度已更新。')
    if attachment:
        message.add_attachment(b'synthetic-only', maintype='application', subtype='octet-stream', filename='说明.txt')
    return message.as_bytes()


class FakeIMAP:
    capabilities = ()

    def __init__(self):
        self.folders = {'INBOX': {1: raw_mail()}, 'Sent Items': {2: raw_mail('发送副本', html=True, attachment=True)}}
        self.validity = b'123'
        self.calls = []
        self.selected = ''
        self.on_fetch = None
        self.fail_select = set()

    def login(self, email, password):
        self.calls.append(('login', email, password))
        return 'OK', []

    def starttls(self, **kwargs):
        assert kwargs['ssl_context'].verify_mode == ssl.CERT_REQUIRED
        self.calls.append(('starttls',))
        return 'OK', []

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Sent \\HasNoChildren) "/" "Sent Items"']

    def select(self, folder, readonly):
        assert readonly is True
        self.selected = folder[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        self.calls.append(('select', self.selected, readonly))
        return ('NO', []) if self.selected in self.fail_select else ('OK', [b'2'])

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [self.validity]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == 'search':
            return 'OK', [' '.join(map(str, self.folders[self.selected])).encode()]
        assert command == 'fetch'
        assert 'BODY.PEEK[' in args[1]
        if self.on_fetch:
            callback, self.on_fetch = self.on_fetch, None
            callback()
        result = []
        for uid in map(int, args[0].split(',')):
            raw = self.folders[self.selected].get(uid)
            if raw is None:
                continue
            header = f'1 (UID {uid} FLAGS () RFC822.SIZE {len(raw)} INTERNALDATE "06-Oct-2026 09:30:00 +0800" '.encode()
            if 'HEADER.FIELDS' in args[1]:
                body = raw.split(b'\n\n', 1)[0] + b'\n\n'
            else:
                body = raw[:mb.BODY_LIMIT]
            result.append((header, body))
            result.append(b')')
        return 'OK', result

    def logout(self):
        self.calls.append(('logout',))
        return 'BYE', []


@pytest.fixture
def server(monkeypatch):
    server = FakeIMAP()
    def factory(host, port, **kwargs):
        assert host == 'imap.example.com'
        assert kwargs['timeout'] == 10
        if 'ssl_context' in kwargs:
            assert kwargs['ssl_context'].check_hostname
            assert kwargs['ssl_context'].verify_mode == ssl.CERT_REQUIRED
        return server
    monkeypatch.setattr(mb.imaplib, 'IMAP4_SSL', factory)
    monkeypatch.setattr(mb.imaplib, 'IMAP4', factory)
    return server


def account(client, email='team@example.com', **settings):
    response = client.post('/api/sender-configs', json=dict(email=email, auth_code='generated-test-secret',
        smtp_host='smtp.example.com', imap_host='imap.example.com', **settings))
    assert response.status_code == 200
    assert 'auth_code' not in response.json()
    return response.json()['id']


def sync(client, sender):
    response = client.post(f'/api/mailbox/sync/{sender}')
    assert response.status_code == 200, response.text
    return response.json()


def test_aggregate_accounts_filters_pagination_and_disabled_senders(client, server):
    one, two = account(client), account(client, 'second@example.com')
    client.patch(f'/api/sender-configs/{two}/toggle')
    assert sync(client, one)['added'] == 2
    assert sync(client, two)['added'] == 2
    page = client.get('/api/mailbox/messages?page_size=2').json()
    assert page['total'] == 4 and len(page['items']) == 2
    assert len(client.get('/api/mailbox/messages?page_size=2&page=2').json()['items']) == 2
    incoming = client.get(f'/api/mailbox/messages?sender_id={one}&kind=inbox').json()
    assert incoming['total'] == 1 and incoming['items'][0]['account_email'] == 'team@example.com'
    assert client.get('/api/mailbox/messages', params={'query': '发送副本'}).json()['total'] == 2
    assert client.get('/api/mailbox/messages', params={'query': "%' OR 1=1 --"}).json()['total'] == 0
    assert all('body' not in row for row in page['items'])
    assert len(server.calls) and all(row[0] not in {'store', 'expunge', 'append'} for row in server.calls)


def test_plain_html_body_safety_attachments_and_offline_cache(client, server, monkeypatch):
    sender = account(client)
    sync(client, sender)
    mail = client.get('/api/mailbox/messages?kind=sent').json()['items'][0]
    response = client.get(f"/api/mailbox/messages/{mail['id']}")
    assert response.status_code == 200
    body = response.json()
    assert '你好 & world' in body['body']
    assert not any(value in body['body'] for value in ['bad()', '<img', 'tracker', 'hidden'])
    assert body['attachments'] == ['说明.txt']
    assert body['unread'] == 1
    assert not body['truncated']
    monkeypatch.setattr(mb.imaplib, 'IMAP4_SSL', Mock(side_effect=AssertionError('Cached body must be offline')))
    assert client.get(f"/api/mailbox/messages/{mail['id']}").json() == body


def test_new_first_backfill_idempotency_and_deletions(client, server):
    server.folders['INBOX'] = {i: raw_mail(f'Message {i}') for i in range(1, 206)}
    sender = account(client)
    first = sync(client, sender)
    assert first['added'] == 101 and first['remaining'] == 105
    with main.db() as conn:
        assert conn.execute("SELECT MIN(uid) FROM mailbox_messages WHERE folder_kind='inbox'").fetchone()[0] == 106
    server.folders['INBOX'][206] = raw_mail('Newest')
    assert sync(client, sender)['added'] == 100
    assert sync(client, sender)['added'] == 6
    assert sync(client, sender)['added'] == 0
    assert client.get('/api/mailbox/messages').json()['total'] == 207
    del server.folders['INBOX'][205]
    sync(client, sender)
    assert client.get('/api/mailbox/messages').json()['total'] == 206


def test_uidvalidity_replaces_old_cache_and_detail_rejects_mismatch(client, server):
    sender = account(client)
    sync(client, sender)
    original = client.get('/api/mailbox/messages').json()['items'][0]['id']
    server.validity = b'999'
    assert client.get(f'/api/mailbox/messages/{original}').status_code == 409
    sync(client, sender)
    assert client.get('/api/mailbox/messages').json()['total'] == 2
    assert client.get(f'/api/mailbox/messages/{original}').status_code == 404


def test_credential_failure_is_redacted_and_other_account_works(client, server, monkeypatch):
    one, two = account(client), account(client, 'good@example.com')
    real_login = server.login
    def login(email, password):
        if email == 'team@example.com':
            raise RuntimeError('sensitive provider reply: generated-test-secret private-body')
        return real_login(email, password)
    monkeypatch.setattr(server, 'login', login)
    failed = sync(client, one)
    assert failed['error'] and 'generated-test-secret' not in json.dumps(failed)
    assert sync(client, two)['added'] == 2
    response = client.get('/api/mailbox/accounts')
    assert 'generated-test-secret' not in response.text and 'private-body' not in response.text


def test_missing_sent_folder_does_not_block_inbox(client, server, monkeypatch):
    monkeypatch.setattr(server, 'list', lambda: ('OK', [b'(\\HasNoChildren) "/" "INBOX"']))
    result = sync(client, account(client))
    assert result['added'] == 1 and '已发送文件夹' in result['error']


def test_unavailable_inbox_does_not_block_sent(client, server):
    server.fail_select.add('INBOX')
    result = sync(client, account(client))
    assert result['added'] == 1 and '收件箱' in result['error']


def test_settings_legacy_update_preserved_cache_purged_on_change_and_delete(client, server):
    sender = account(client)
    sync(client, sender)
    payload = dict(email='team@example.com', smtp_host='smtp.example.com', smtp_port=465, note='new note', auth_code='')
    updated = client.put(f'/api/sender-configs/{sender}', json=payload).json()
    assert updated['imap_host'] == 'imap.example.com'
    assert client.get('/api/mailbox/messages').json()['total'] == 2
    payload['email'] = 'changed@example.com'
    assert client.put(f'/api/sender-configs/{sender}', json=payload).status_code == 200
    assert client.get('/api/mailbox/messages').json()['total'] == 0
    sync(client, sender)
    assert client.delete(f'/api/sender-configs/{sender}').status_code == 200
    assert client.get('/api/mailbox/messages').json()['total'] == 0
    assert client.get('/api/mailbox/accounts').json() == []


@pytest.mark.parametrize('change', ['delete', 'edit'])
def test_no_stale_sync_write_after_concurrent_account_change(client, server, change):
    sender = account(client)
    def modify():
        with main.db() as conn:
            mb.clear_account(conn, sender)
            if change == 'delete':
                conn.execute('DELETE FROM sender_configs WHERE id=?', (sender,))
            else:
                conn.execute('UPDATE sender_configs SET mailbox_revision=mailbox_revision+1 WHERE id=?', (sender,))
            conn.commit()
    server.on_fetch = modify
    assert client.post(f'/api/mailbox/sync/{sender}').status_code == 409
    assert client.get('/api/mailbox/messages').json()['total'] == 0


def test_concurrent_sync_returns_busy(client, server):
    sender = account(client)
    with main.MAILBOX.account_lock(sender):
        assert client.post(f'/api/mailbox/sync/{sender}').status_code == 409


def test_starttls_before_authentication(client, server):
    sender = account(client, imap_security='starttls', imap_port=143)
    assert sync(client, sender)['error'] == ''
    assert server.calls[0][0] == 'starttls' and server.calls[1][0] == 'login'


def test_no_cleartext_fallback_when_starttls_fails(client, server, monkeypatch):
    monkeypatch.setattr(server, 'starttls', lambda **kwargs: ('NO', []))
    sender = account(client, imap_security='starttls', imap_port=143)
    assert sync(client, sender)['error']
    assert not any(call[0] == 'login' for call in server.calls)


@pytest.mark.parametrize('settings', [dict(imap_host='imap.example.com\r\nBAD'), dict(imap_host='https://imap.example.com'), dict(imap_port=0), dict(imap_security='none'), dict(imap_sent_folder='Sent\r\nBAD')])
def test_invalid_imap_settings_rejected(client, settings):
    response = client.post('/api/sender-configs', json=dict(email='team@example.com', auth_code='not-a-real-secret', **settings))
    assert response.status_code == 422


def test_localized_quoted_and_literal_folder_discovery():
    client = FakeIMAP()
    client.list = lambda: ('OK', [('(\\Sent) "/" {8}'.encode(), b'A " B')])
    assert mb.sent_folder(client, '') == 'A " B'
    assert mb.quote_folder('A " B') == '"A \\" B"'
    name = '已发送 & 合作'
    assert mb.decode_folder(mb.encode_folder(name)) == name
    client.list = lambda: ('OK', [f'(\\HasNoChildren) "/" "{mb.encode_folder(name.split(" & ")[0])}"'.encode()])
    assert mb.sent_folder(client, '') == mb.encode_folder('已发送')


def test_unknown_provider_requires_configuration_without_connection(client):
    response = client.post('/api/sender-configs', json=dict(email='team@example.com', auth_code='synthetic-only', smtp_host='smtp.unknown.example.com'))
    result = sync(client, response.json()['id'])
    assert 'IMAP 服务器' in result['error']
    assert not client.get('/api/mailbox/accounts').json()[0]['configured']


def test_large_body_truncation_is_explicit(client, server, monkeypatch):
    monkeypatch.setattr(mb, 'BODY_LIMIT', 350)
    server.folders['INBOX'][1] = raw_mail('Long') + b'x' * 1000
    sync(client, account(client))
    mail = client.get('/api/mailbox/messages?kind=inbox').json()['items'][0]
    assert client.get(f"/api/mailbox/messages/{mail['id']}").json()['truncated']


def test_summary_requires_session_and_valid_filters(client):
    assert client.get('/api/mailbox/accounts', headers={'Authorization': ''}).status_code == 401
    assert client.get('/api/mailbox/messages?kind=unknown').status_code == 422
    assert client.get('/api/mailbox/messages?page=0').status_code == 422
    assert client.post('/api/mailbox/sync/999').status_code == 404


def test_upgrade_makes_backup_and_preserves_existing_senders(client):
    sender = account(client)
    with main.db() as conn:
        for name in ['imap_host', 'imap_port', 'imap_security', 'imap_sent_folder', 'mailbox_revision']:
            conn.execute(f'ALTER TABLE sender_configs DROP COLUMN {name}')
        conn.commit()
    main.init_db()
    import pathlib
    assert (pathlib.Path(main.DATA_DIR) / 'before-v3.2.sqlite3').exists()
    stored = client.get('/api/sender-configs').json()[0]
    assert stored['id'] == sender and stored['imap_port'] == 993 and stored['has_auth_code']


def test_successful_local_sends_are_readable_offline_and_remote_copies_merge(client, server, monkeypatch):
    from test_desktop import setup_task, submit, wait_done
    payload = setup_task(client)
    sender = payload['sender_ids'][0]
    monkeypatch.setattr(main, 'send_email', lambda *args: '<synthetic@example.com>')
    done = wait_done(client, submit(client, payload).json()['task_id'])
    assert done['success'] == 1
    rows = client.get('/api/mailbox/messages?kind=sent').json()
    assert rows['total'] == 1 and rows['items'][0]['source'] == 'local'
    detail = client.get(f"/api/mailbox/messages/{rows['items'][0]['id']}").json()
    assert detail['body'] == payload['content']['body']
    # Updating the IMAP endpoint does not remove the successful local record.
    current = client.get('/api/sender-configs').json()[0]
    current.update(imap_host='imap.example.com', auth_code='')
    assert client.put(f'/api/sender-configs/{sender}', json=current).status_code == 200
    assert client.get('/api/mailbox/messages').json()['total'] == 1
    sync(client, sender)
    merged = client.get('/api/mailbox/messages?kind=sent').json()
    assert merged['total'] == 1 and merged['items'][0]['source'] == 'imap'
    # A removed remote copy reveals the preserved local record again.
    server.folders['Sent Items'] = {}
    sync(client, sender)
    assert client.get('/api/mailbox/messages?kind=sent').json()['items'][0]['source'] == 'local'


def test_random_local_archive_uses_actual_chosen_template_and_backfills_once(client, monkeypatch):
    from test_random_templates import random_payload
    from test_desktop import submit, wait_done
    payload, saved = random_payload(client)
    monkeypatch.setattr(main.random, 'choice', lambda pool: pool[1])
    monkeypatch.setattr(main, 'send_email', lambda *args: '<local-test@example.com>')
    assert wait_done(client, submit(client, payload).json()['task_id'])['success'] == 1
    with main.db() as conn:
        conn.execute('DELETE FROM mailbox_messages')
        conn.execute('DELETE FROM mailbox_migrations')
        conn.commit()
    main.init_db()
    main.init_db()
    rows = client.get('/api/mailbox/messages').json()
    assert rows['total'] == 1 and rows['items'][0]['subject'] == saved[1]['subject']
    detail = client.get(f"/api/mailbox/messages/{rows['items'][0]['id']}").json()
    assert detail['body'] == 'HTML B' and detail['attachments'] == ['same-name.txt']


def test_failed_send_is_not_misrepresented_as_sent_mail(client, monkeypatch):
    from test_desktop import setup_task, submit, wait_done
    payload = setup_task(client)
    monkeypatch.setattr(main, 'send_email', Mock(side_effect=RuntimeError('synthetic SMTP failure')))
    assert wait_done(client, submit(client, payload).json()['task_id'])['failed'] == 1
    assert client.get('/api/mailbox/messages').json()['total'] == 0


def test_real_imap_wire_protocol_on_loopback_only(client, monkeypatch):
    """Exercise stdlib protocol parsing with a local synthetic server, never a real mailbox."""
    import contextlib
    import socketserver
    import threading
    # conftest replaces the public constructors with network guards, but the original
    # SSL subclass retains IMAP4 as its base. This test deliberately uses plain loopback.
    from imaplib import IMAP4_stream
    real_imap = IMAP4_stream.__bases__[0]
    raw = raw_mail().replace(b'\n', b'\r\n')
    calls = []

    class Protocol(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'* OK local synthetic IMAP ready\r\n')
            while line := self.rfile.readline():
                tag, command, *rest = line.rstrip(b'\r\n').split(b' ', 2)
                args = rest[0] if rest else b''
                calls.append((command, args))
                if command == b'CAPABILITY':
                    self.wfile.write(b'* CAPABILITY IMAP4rev1\r\n')
                elif command == b'LIST':
                    self.wfile.write(b'* LIST (\\Sent) "/" "Sent Items"\r\n')
                elif command == b'EXAMINE':
                    self.wfile.write(b'* 1 EXISTS\r\n* OK [UIDVALIDITY 42] stable\r\n')
                elif command == b'UID' and args.startswith(b'SEARCH'):
                    self.wfile.write(b'* SEARCH 7\r\n')
                elif command == b'UID' and args.startswith(b'FETCH'):
                    payload = raw.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n' if b'HEADER.FIELDS' in args else raw
                    field = b'BODY[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID)]' if b'HEADER.FIELDS' in args else b'BODY[]<0>'
                    self.wfile.write(b'* 1 FETCH (UID 7 FLAGS () RFC822.SIZE ' + str(len(raw)).encode() + b' ' + field + b' {' + str(len(payload)).encode() + b'}\r\n' + payload + b')\r\n')
                elif command == b'LOGOUT':
                    self.wfile.write(b'* BYE closing\r\n' + tag + b' OK bye\r\n')
                    break
                elif command != b'LOGIN':
                    self.wfile.write(tag + b' BAD unsupported command\r\n')
                    continue
                self.wfile.write(tag + b' OK complete\r\n')

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Protocol) as tcp:
        thread = threading.Thread(target=tcp.serve_forever, daemon=True)
        thread.start()
        @contextlib.contextmanager
        def local_connection(account):
            with real_imap('127.0.0.1', tcp.server_address[1], timeout=2) as session:
                session.login(account['email'], account['auth_code'])
                yield session
        monkeypatch.setattr(mb, 'connect', local_connection)
        try:
            assert sync(client, account(client))['added'] == 2
            mail = client.get('/api/mailbox/messages').json()['items'][0]
            assert '项目进度已更新' in client.get(f"/api/mailbox/messages/{mail['id']}").json()['body']
            assert not any(command in {b'SELECT', b'STORE', b'APPEND', b'EXPUNGE'} for command, _ in calls)
            assert all(b'BODY.PEEK[' in args for command, args in calls if command == b'UID' and args.startswith(b'FETCH'))
        finally:
            tcp.shutdown()
            thread.join(timeout=3)
