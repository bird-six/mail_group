import json
from itertools import cycle
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import main
from test_desktop import setup_task, submit, wait_done


def templates(client):
    return [client.post('/api/saved-mails', data={
        'subject': label, 'body': body, 'content_type': kind,
    }, files={'attachments': ('same-name.txt', data, 'text/plain')}).json()
        for label, body, kind, data in [('A', 'Plain A', 'plain', b'A attachment'),
                                        ('B', '<b>HTML B</b>', 'html', b'B attachment')]]


def random_payload(client, addresses=None):
    payload = setup_task(client, addresses)
    payload.pop('content')
    saved = templates(client)
    payload['template_ids'] = [item['id'] for item in saved]
    return payload, saved


def test_each_recipient_draws_from_selected_templates_with_matching_attachments(client, monkeypatch):
    payload, saved = random_payload(client, [f'r{i}@example.com' for i in range(4)])
    indices = cycle([0, 1])
    calls = []
    def choose(pool):
        assert [item.template_id for item in pool] == payload['template_ids']
        calls.append(True)
        return pool[next(indices)]
    monkeypatch.setattr(main.random, 'choice', choose)
    sent = {}
    monkeypatch.setattr(main, 'send_email', lambda sender, recipient, content, files:
        sent.update({recipient: (content.subject, content.body, content.content_type, files[0].filename, files[0].content)}))
    task = submit(client, payload)
    assert task.status_code == 200
    task_id = task.json()['task_id']
    done = wait_done(client, task_id)
    assert len(calls) == 4 and done['success'] == 4
    assert done['subject'] == '随机模板（2个）'
    for i, item in enumerate(done['assignments']):
        expected = saved[i % 2]
        assert item['template_id'] == expected['id'] and item['subject'] == expected['subject']
        assert sent[item['recipient']] == (expected['subject'], expected['body'], expected['content_type'],
                                          'same-name.txt', b'A attachment' if i % 2 == 0 else b'B attachment')
    for item in saved:
        assert client.delete(f"/api/saved-mails/{item['id']}").status_code == 200
    replay = submit(client, payload)
    assert replay.status_code == 200 and replay.json()['task_id'] == task_id
    assert len(calls) == 4  # An idempotent retry never redraws or resends.


def test_interruption_and_deleted_templates_preserve_choices_and_copied_content(client, monkeypatch):
    worker = main.run_task
    monkeypatch.setattr(main, 'run_task', AsyncMock())
    indices = cycle([0, 1])
    monkeypatch.setattr(main.random, 'choice', lambda pool: pool[next(indices)])
    payload, saved = random_payload(client, ['sent@example.com', 'pending@example.com', 'unknown@example.com'])
    created = submit(client, payload).json()
    task_id = created['task_id']
    for item in saved:
        client.delete(f"/api/saved-mails/{item['id']}")
    with main.db() as conn:
        items = created['assignments']
        items[0]['status'] = 'success'; items[2]['status'] = 'sending'
        conn.execute('UPDATE task_runs SET assignments=?,status=? WHERE task_id=?', (json.dumps(items), 'running', task_id))
        conn.commit()
    main.TASKS.clear(); main.WORKERS.clear(); main.init_db()
    def do_not_redraw(_pool):
        raise AssertionError('Restart must use the saved draw')
    monkeypatch.setattr(main.random, 'choice', do_not_redraw)
    monkeypatch.setattr(main, 'run_task', worker)
    sent = []
    monkeypatch.setattr(main, 'send_email', lambda sender, recipient, content, files:
        sent.append((recipient, content.subject, content.body, files[0].content)))
    assert client.post(f'/api/tasks/{task_id}/resume', json={}).status_code == 200
    paused = wait_done(client, task_id, 'interrupted')
    assert paused['uncertain'] == 1 and paused['success'] == 2
    assert sent == [('pending@example.com', 'B', '<b>HTML B</b>', b'B attachment')]
    assert client.post(f'/api/tasks/{task_id}/resume', json={'retry_uncertain': True}).status_code == 200
    done = wait_done(client, task_id)
    assert done['success'] == 3
    assert sent[-1] == ('unknown@example.com', 'A', 'Plain A', b'A attachment')
    assert [item['template_id'] for item in done['assignments']] == [item['template_id'] for item in created['assignments']]


@pytest.mark.parametrize('invalid_ids', [[], [999999], [1, 1], [True], ['1'], [1.0], [-1], [2**63]])
def test_invalid_template_pool_creates_no_task(client, monkeypatch, invalid_ids):
    monkeypatch.setattr(main, 'run_task', AsyncMock())
    payload, _ = random_payload(client)
    payload['template_ids'] = invalid_ids
    assert submit(client, payload).status_code in (400, 409)
    assert client.get('/api/tasks').json() == []
    assert not (Path(main.DATA_DIR) / 'task_attachments').exists()
    main.run_task.assert_not_called()


def test_mixed_content_or_editor_attachments_are_rejected(client):
    payload, _ = random_payload(client)
    assert submit(client, {**payload, 'content': {'subject': 'other', 'body': 'other'}}).status_code == 400
    assert submit(client, payload, files={'attachments': ('other.txt', b'other')}).status_code == 400
    assert client.get('/api/tasks').json() == []


def test_missing_attachment_in_any_selected_template_blocks_whole_task(client, monkeypatch):
    monkeypatch.setattr(main, 'run_task', AsyncMock())
    payload, saved = random_payload(client)
    (Path(main.SAVED_ATTACHMENTS_DIR) / saved[1]['attachments'][0]).unlink()
    assert submit(client, payload).status_code == 409
    assert client.get('/api/tasks').json() == []
    main.run_task.assert_not_called()


def test_single_template_and_attachment_free_template(client, monkeypatch):
    payload = setup_task(client)
    payload.pop('content')
    saved = client.post('/api/saved-mails', data={'subject': 'Solo', 'body': 'One template'}).json()
    payload['template_ids'] = [saved['id']]
    sent = []
    monkeypatch.setattr(main, 'send_email', lambda sender, recipient, content, files: sent.append((content.subject, files)))
    task_id = submit(client, payload).json()['task_id']
    assert wait_done(client, task_id)['success'] == 1
    assert sent == [('Solo', [])]


def test_v3_manual_task_remains_resumable_after_schema_upgrade(client, monkeypatch):
    worker = main.run_task
    monkeypatch.setattr(main, 'run_task', AsyncMock())
    payload = setup_task(client)
    task_id = submit(client, payload).json()['task_id']
    with main.db() as conn:
        conn.execute('ALTER TABLE task_runs DROP COLUMN template_snapshots')
        conn.commit()
    main.TASKS.clear(); main.WORKERS.clear(); main.init_db()
    assert (Path(main.DATA_DIR) / 'before-v3.1.sqlite3').is_file()
    monkeypatch.setattr(main, 'run_task', worker)
    sent = []
    monkeypatch.setattr(main, 'send_email', lambda sender, recipient, content, files: sent.append(content.subject))
    assert client.post(f'/api/tasks/{task_id}/resume', json={}).status_code == 200
    assert wait_done(client, task_id)['success'] == 1
    assert sent == ['Offline test']
