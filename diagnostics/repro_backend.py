"""Offline JSON-lines bridge: real FastAPI routes, temporary DB, no SMTP."""
from __future__ import annotations

import json
from pathlib import Path
import smtplib
import imaplib
import sys
import tempfile
import types
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sys.path.insert(0, str(ROOT / 'backend'))
    source = (ROOT / "backend/main.py").read_text(encoding="utf-8")
    # The only initialization change: never open the user's production database.
    source = source.replace("\ninit_db()\n", "\n# initialized in temporary directory\n")
    module = types.ModuleType("offline_backend")
    module.__file__ = str(ROOT / "backend/main.py")
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    smtp_guard = Mock(side_effect=AssertionError("Real SMTP is forbidden in this reproduction"))
    smtplib.SMTP = smtp_guard
    smtplib.SMTP_SSL = smtp_guard
    module.send_email = smtp_guard
    imap_guard = Mock(side_effect=AssertionError('Real IMAP is forbidden in this reproduction'))
    imaplib.IMAP4 = imap_guard
    imaplib.IMAP4_SSL = imap_guard
    module.run_task = AsyncMock()  # Task creation is tested; the actual worker is never run.

    with tempfile.TemporaryDirectory(prefix="mail-bulk-repro-") as tmp:
        module.DATA_DIR = tmp
        module.DB_PATH = str(Path(tmp) / "test.sqlite3")
        module.SAVED_ATTACHMENTS_DIR = str(Path(tmp) / "attachments")
        module.init_db()
        headers = {"Authorization": f"Bearer {module.API_TOKEN}"} if hasattr(module, "API_TOKEN") else {}
        with TestClient(module.app, headers=headers) as client:
            for raw in sys.stdin:
                command = json.loads(raw)
                if command.get("op") == "close":
                    break
                if command.get("op") == "reset":
                    with module.db() as conn:
                        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        for table in ("recipients", "sender_configs", "saved_mails", "task_history", "send_log", "task_runs", "mailbox_messages", "mailbox_folders", "mailbox_sync_state"):
                            if table not in tables:
                                continue
                            conn.execute(f"DELETE FROM {table}")
                        conn.commit()
                    module.TASKS.clear()
                    module.run_task.reset_mock()
                    client.post("/api/sender-configs", json={"email": "simulation@example.com", "auth_code": "unused-test-code"})
                    client.post("/api/saved-mails", data={"subject": "Same template for every case", "body": "No real messages are sent"})
                    if command.get("recipients"):
                        with module.db() as conn:
                            conn.executemany("INSERT INTO recipients (email,enabled,note,created_at) VALUES (?,1,'legacy record','2026-01-01')", [(email,) for email in command["recipients"]])
                            conn.commit()
                    result = {"status": 200, "text": "{}"}
                else:
                    kwargs = {}
                    if "json" in command:
                        kwargs["json"] = command["json"]
                    if "form" in command:
                        kwargs["data"] = command["form"]
                    result = {}
                    # Capture the exact exception the legacy route hides from its response.
                    if command["path"] == "/api/tasks" and command["method"] == "POST":
                        try:
                            module.SendTaskRequest.model_validate_json(command["form"]["payload"])
                        except ValidationError as exc:
                            result["validation_errors"] = exc.errors(include_context=False, include_input=False, include_url=False)
                    response = client.request(command["method"], command["path"], **kwargs)
                    result.update(status=response.status_code, text=response.text)
                result.update(smtp_calls=smtp_guard.call_count, scheduled_tasks=module.run_task.call_count)
                assert smtp_guard.call_count == 0
                assert imap_guard.call_count == 0
                print(json.dumps(result, ensure_ascii=True), flush=True)
        if module._shared_conn is not None:
            module._shared_conn.close()


if __name__ == "__main__":
    main()
