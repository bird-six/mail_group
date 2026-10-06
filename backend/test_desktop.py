import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
import main


def setup_task(client, recipients=None, request_id='audit-request'):
    recipients = recipients or ['recipient@example.com']
    sender = client.post('/api/sender-configs', json={'email': 'sender@example.com', 'auth_code': 'private-test-code'}).json()
    client.post('/api/recipients/batch', json=[{'email': address} for address in recipients])
    return {'content': {'subject': 'Offline test', 'body': 'No real email'}, 'sender_ids': [sender['id']],
            'recipients': [{'email': address} for address in recipients], 'request_id': request_id}


def submit(client, payload, **kwargs):
    return client.post('/api/tasks', data={'payload': json.dumps(payload)}, **kwargs)


def wait_done(client, task_id, status='completed'):
    for _ in range(200):
        row = client.get(f'/api/tasks/{task_id}').json()
        if row.get('status') == status:
            return row
        time.sleep(.01)
    raise AssertionError(row)


@pytest.mark.parametrize('route', ['/api/sender-configs', '/api/recipients', '/api/tasks', '/api/stats',
    '/api/saved-mails', '/api/saved-mails/1/attachments/file.txt', '/api/%73ender-configs', '/api/tasks/'])
def test_api_rejects_anonymous_and_wrong_token(client, route):
    with TestClient(main.app) as anonymous:
        assert anonymous.get(route).status_code == 401
        assert anonymous.get(route, headers={'Authorization': 'Bearer wrong'}).status_code == 401


def test_untrusted_origin_cannot_read_or_write_even_with_token(client):
    headers = {'Origin': 'https://untrusted.example'}
    assert client.get('/api/sender-configs', headers=headers).status_code == 403
    assert client.post('/api/saved-mails', data={'subject': 'test', 'body': 'test'}, headers=headers).status_code == 403
    assert client.options('/api/saved-mails', headers={**headers, 'Access-Control-Request-Method': 'POST'}).status_code == 403
    assert client.get('/api/saved-mails').json() == []


def test_credentials_never_returned_and_empty_edit_preserves_secret(client):
    created = client.post('/api/sender-configs', json={'email': 'sender@example.com', 'auth_code': 'private-test-code'})
    assert 'private-test-code' not in created.text and 'auth_code"' not in created.text.replace('has_auth_code"', '')
    sid = created.json()['id']
    updated = client.put(f'/api/sender-configs/{sid}', json={'email': 'sender@example.com', 'auth_code': '', 'note': 'edited'})
    assert updated.status_code == 200 and 'private-test-code' not in updated.text
    with main.db() as conn:
        assert conn.execute('SELECT auth_code FROM sender_configs WHERE id=?', (sid,)).fetchone()[0] == 'private-test-code'
    assert 'private-test-code' not in client.get('/api/sender-configs').text
    bad = client.post('/api/assignments/preview', json={'senders': [{'auth_code': 'private-test-code'}]})
    assert bad.status_code == 422 and 'private-test-code' not in bad.text
    bad_create = client.post('/api/sender-configs', json={'email': 'bad', 'auth_code': 'private-test-code', 'smtp_port': 999999})
    assert bad_create.status_code == 422 and 'private-test-code' not in bad_create.text


def test_completed_details_idempotency_and_stale_recipient_block(client, monkeypatch):
    worker = Mock()
    monkeypatch.setattr(main, 'send_email', worker)
    payload = setup_task(client)
    created = submit(client, payload)
    assert created.status_code == 200
    tid = created.json()['task_id']
    done = wait_done(client, tid)
    assert done['success'] == 1 and done['assignments'][0]['status'] == 'success'
    replay = submit(client, payload)
    assert replay.status_code == 200 and replay.json()['task_id'] == tid
    assert worker.call_count == 1
    conflicting = {**payload, 'content': {'subject': 'Changed', 'body': 'same'}}
    assert submit(client, conflicting).status_code == 409
    assert submit(client, {**payload, 'request_id': 'new-request'}).status_code == 409
    assert client.get('/api/tasks').json()[0]['assignments'][0]['recipient'] == 'recipient@example.com'


def test_recipient_reserved_across_pending_tasks(client, monkeypatch):
    monkeypatch.setattr(main, 'run_task', AsyncMock())
    payload = setup_task(client)
    assert submit(client, payload).status_code == 200
    assert submit(client, {**payload, 'request_id': 'another'}).status_code == 409
    assert len(client.get('/api/tasks').json()) == 1


@pytest.mark.parametrize('mode', ['missing', 'disabled', 'duplicate'])
def test_sender_references_fail_before_creating_task(client, monkeypatch, mode):
    worker = AsyncMock()
    monkeypatch.setattr(main, 'run_task', worker)
    payload = setup_task(client)
    if mode == 'missing': payload['sender_ids'] = [999999]
    if mode == 'disabled': client.patch(f"/api/sender-configs/{payload['sender_ids'][0]}/toggle")
    if mode == 'duplicate': payload['sender_ids'] *= 2
    assert submit(client, payload).status_code in (400, 409)
    assert client.get('/api/tasks').json() == []
    worker.assert_not_called()


def test_sender_rate_limit_applies_across_tasks(client, monkeypatch):
    monkeypatch.setattr(main, 'SEND_INTERVAL', (.06, .06))
    starts = []
    def fake_send(*args):
        starts.append(time.monotonic())
        time.sleep(.01)
    monkeypatch.setattr(main, 'send_email', fake_send)
    payload = setup_task(client, ['one@example.com', 'two@example.com'])
    first = submit(client, {**payload, 'recipients': [{'email': 'one@example.com'}]}).json()
    second = submit(client, {**payload, 'recipients': [{'email': 'two@example.com'}], 'request_id': 'task-2'}).json()
    wait_done(client, first['task_id']); wait_done(client, second['task_id'])
    assert len(starts) == 2 and starts[1] - starts[0] >= .06


def test_durable_resume_does_not_resend_success_or_uncertain(client, monkeypatch):
    worker = main.run_task
    monkeypatch.setattr(main, 'run_task', AsyncMock())
    payload = setup_task(client, ['sent@example.com', 'pending@example.com', 'unknown@example.com'])
    tid = submit(client, payload, files={'attachments': ('resume.txt', b'saved bytes', 'text/plain')}).json()['task_id']
    with main.db() as conn:
        row = conn.execute('SELECT assignments FROM task_runs WHERE task_id=?', (tid,)).fetchone()
        items = json.loads(row[0]); items[0]['status'] = 'success'; items[2]['status'] = 'sending'
        conn.execute('UPDATE task_runs SET assignments=?,status=? WHERE task_id=?', (json.dumps(items), 'running', tid)); conn.commit()
    main.TASKS.clear(); main.WORKERS.clear()
    main.init_db()  # Restart recovery converts in-flight state to uncertain.
    assert client.get(f'/api/tasks/{tid}').json()['uncertain'] == 1
    delivered = []
    def fake_send(sender, recipient, content, attachments):
        assert attachments[0].content == b'saved bytes'
        delivered.append(recipient)
    monkeypatch.setattr(main, 'run_task', worker)
    monkeypatch.setattr(main, 'send_email', fake_send)
    assert client.post(f'/api/tasks/{tid}/resume', json={}).status_code == 200
    paused = wait_done(client, tid, 'interrupted')
    assert paused['success'] == 2 and paused['uncertain'] == 1
    assert delivered == ['pending@example.com']
    assert client.post(f'/api/tasks/{tid}/resume', json={}).status_code == 409
    assert client.post(f'/api/tasks/{tid}/resume', json={'retry_uncertain': True}).status_code == 200
    assert wait_done(client, tid)['success'] == 3
    assert delivered == ['pending@example.com', 'unknown@example.com']


def test_statistics_use_full_dates_and_half_open_previous_period(client, monkeypatch):
    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 10, 6, 12)
    monkeypatch.setattr(main, 'datetime', FixedDate)
    for date, status in [('2025-10-06T10:00:00', 'success'), ('2026-10-06T10:00:00', 'failed'), ('2026-09-30T23:59:59', 'success')]:
        main.record_send_log('stats', 'a@example.com', 'sender@example.com', status, 'test', date)
    stats = client.get('/api/stats?period=day').json()
    assert stats['stats'] == [{'label': '2026-10-06', 'total': 1, 'success': 0, 'failed': 1}]
    assert stats['success_prev'] == 1 and stats['period_failed'] == 1
    assert client.get('/api/stats?period=hour').json()['stats'][0]['label'] == '2026-10-06 10:00'


def test_template_validation_html_and_encoded_download(client, monkeypatch):
    payload = setup_task(client)
    for name in ['bad.exe', 'bad.exe.', 'bad.exe ', '../bad.txt', 'folder\\bad.txt']:
        files = {'attachments': (name, b'harmless', 'application/octet-stream')}
        assert client.post('/api/saved-mails', data={'subject': 'x', 'body': 'x'}, files=files).status_code == 400
        assert submit(client, payload, files=files).status_code == 400
    monkeypatch.setattr(main, 'MAX_ATTACHMENT_SIZE', 10)
    assert client.post('/api/saved-mails', data={'subject': 'x', 'body': 'x'}, files={'attachments': ('big.txt', b'x'*11)}).status_code == 400
    saved = client.post('/api/saved-mails', data={'subject': 'HTML', 'body': '<b>Hello</b>', 'content_type': 'html'}, files={'attachments': ('中文 #1.txt', b'hello', 'text/plain')}).json()
    from urllib.parse import quote
    download = client.get(f"/api/saved-mails/{saved['id']}/attachments/{quote(saved['attachments'][0])}")
    assert download.content == b'hello'
    assert client.get(f"/api/saved-mails/{saved['id']}").json()['content_type'] == 'html'
    smtp = Mock(); smtp.__enter__ = Mock(return_value=smtp); smtp.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(main, '_create_smtp_connection', Mock(return_value=smtp))
    main.send_email(main.resolve_senders(payload['sender_ids'])[0], 'recipient@example.com', main.MailContent(subject='HTML', body='<b>Hello</b>', content_type='html'), [])
    assert smtp.send_message.call_args.args[0].get_content_type() == 'text/html'


def test_duplicate_recipient_edit_returns_conflict_and_connection_recovers(client):
    first = client.post('/api/recipients', json={'email': 'one@example.com'}).json()
    client.post('/api/recipients', json={'email': 'two@example.com'})
    assert client.patch(f"/api/recipients/{first['id']}", json={'email': 'two@example.com'}).status_code == 409
    assert client.patch(f"/api/recipients/{first['id']}", json={'note': 'still usable'}).status_code == 200


@pytest.mark.parametrize('sender_id', [True, 1.0, '1', 0, -1, 2**63])
def test_sender_ids_require_strict_sqlite_positive_integers(client, sender_id):
    payload = setup_task(client)
    payload['sender_ids'] = [sender_id]
    assert client.post('/api/assignments/preview', json=payload).status_code == 422
    assert submit(client, payload).status_code == 400
    assert client.get('/api/tasks').json() == []


def test_legacy_database_upgrade_preserves_data_and_creates_backup(client):
    sender = client.post('/api/sender-configs', json={'email': 'legacy@example.com', 'auth_code': 'legacy-placeholder'}).json()
    client.post('/api/recipients', json={'email': 'keep@example.com', 'note': 'keep this note'})
    template = client.post('/api/saved-mails', data={'subject': 'Legacy template', 'body': 'Keep content'}).json()
    with main.db() as conn:
        conn.execute('DROP TABLE task_runs')
        conn.execute('ALTER TABLE saved_mails DROP COLUMN content_type')
        conn.commit()
    main.init_db()
    assert client.get('/api/sender-configs').json()[0]['id'] == sender['id']
    assert client.get('/api/recipients').json()[0]['note'] == 'keep this note'
    assert client.get(f"/api/saved-mails/{template['id']}").json()['content_type'] == 'plain'
    with sqlite3.connect(Path(main.DATA_DIR) / 'before-v3.sqlite3') as backup:
        assert backup.execute('SELECT count(*) FROM sender_configs').fetchone()[0] == 1
        assert backup.execute("SELECT count(*) FROM sqlite_master WHERE name='task_runs'").fetchone()[0] == 0
        assert backup.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    main.init_db()  # A second launch must not overwrite the original migration backup.
    assert client.get('/api/tasks').json() == []
