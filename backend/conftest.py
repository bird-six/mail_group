"""Every backend test uses a disposable database and blocks real SMTP."""
import asyncio
from pathlib import Path
import smtplib
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import main


@pytest.fixture(autouse=True)
def isolated_backend(monkeypatch, tmp_path):
    if main._shared_conn is not None:
        main._shared_conn.close()
    main._shared_conn = None
    for name, value in {
        'DATA_DIR': str(tmp_path), 'DB_PATH': str(tmp_path / 'test.sqlite3'),
        'SAVED_ATTACHMENTS_DIR': str(tmp_path / 'attachments'),
        'TASKS': {}, 'WORKERS': {}, 'TASK_LOCK': asyncio.Lock(), 'STOPPING': False,
        'SENDER_LOCKS': {}, 'SENDER_NEXT_SEND': {}, 'SEND_INTERVAL': (0, 0), 'API_TOKEN': 'test-session-only',
    }.items():
        monkeypatch.setattr(main, name, value)
    guard = Mock(side_effect=AssertionError('Tests may not connect to real SMTP'))
    monkeypatch.setattr(smtplib, 'SMTP', guard)
    monkeypatch.setattr(smtplib, 'SMTP_SSL', guard)
    main.init_db()
    yield
    if main._shared_conn is not None:
        main._shared_conn.close()
        main._shared_conn = None
    assert guard.call_count == 0


@pytest.fixture
def client(isolated_backend):
    with TestClient(main.app, headers={'Authorization': f'Bearer {main.API_TOKEN}'}) as session:
        yield session
