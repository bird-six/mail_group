from __future__ import annotations

import os
import json
import sys
import tempfile
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

# 确保能找到 backend 目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import init_db


def saved_sender(client, email="sender@example.com", enabled=True):
    sender = client.post("/api/sender-configs", json={"email": email, "auth_code": "test-only"}).json()
    if not enabled:
        client.patch(f"/api/sender-configs/{sender['id']}/toggle")
    return sender["id"]


SENDER_PAYLOAD = {
    "email": "test_sender@example.com",
    "auth_code": "abcdefghijklmnop",
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "note": "测试发送邮箱",
}

RECIPIENT_PAYLOAD = {
    "email": "test_recipient@example.com",
    "note": "测试收件人",
}

MAIL_PAYLOAD = {
    "subject": "测试邮件主题",
    "body": "这是测试邮件的正文内容。",
}


# ── 健康检查 ──────────────────────────────────────────────────────────────


class TestHealth:
    def test_health(self, client: TestClient) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ── 发送邮箱 ──────────────────────────────────────────────────────────────


class TestSenders:
    def test_create_sender(self, client: TestClient) -> None:
        resp = client.post("/api/sender-configs", json=SENDER_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == SENDER_PAYLOAD["email"]
        assert "auth_code" not in data
        assert data["has_auth_code"] is True
        assert data["enabled"] is True
        assert "id" in data

    def test_list_senders(self, client: TestClient) -> None:
        client.post("/api/sender-configs", json=SENDER_PAYLOAD)
        resp = client.get("/api/sender-configs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["email"] == SENDER_PAYLOAD["email"]

    def test_update_sender(self, client: TestClient) -> None:
        create_resp = client.post("/api/sender-configs", json=SENDER_PAYLOAD)
        sid = create_resp.json()["id"]
        updated = {**SENDER_PAYLOAD, "note": "已更新备注"}
        resp = client.put(f"/api/sender-configs/{sid}", json=updated)
        assert resp.status_code == 200
        assert resp.json()["note"] == "已更新备注"

    def test_delete_sender(self, client: TestClient) -> None:
        create_resp = client.post("/api/sender-configs", json=SENDER_PAYLOAD)
        sid = create_resp.json()["id"]
        resp = client.delete(f"/api/sender-configs/{sid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_delete_nonexistent_sender(self, client: TestClient) -> None:
        resp = client.delete("/api/sender-configs/99999")
        assert resp.status_code == 404

    def test_create_sender_empty_email(self, client: TestClient) -> None:
        resp = client.post("/api/sender-configs", json={"email": "", "auth_code": "test"})
        assert resp.status_code == 422

    def test_create_sender_empty_auth_code(self, client: TestClient) -> None:
        resp = client.post("/api/sender-configs", json={"email": "empty@example.com", "auth_code": ""})
        assert resp.status_code == 422


# ── 收件人 ────────────────────────────────────────────────────────────────


class TestRecipients:
    def test_create_recipient(self, client: TestClient) -> None:
        resp = client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == RECIPIENT_PAYLOAD["email"]
        assert data["enabled"] is True
        assert "id" in data

    def test_create_duplicate_recipient(self, client: TestClient) -> None:
        client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        resp = client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        assert resp.status_code == 200
        assert resp.json()["email"] == RECIPIENT_PAYLOAD["email"]

    def test_list_recipients(self, client: TestClient) -> None:
        client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        resp = client.get("/api/recipients")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_toggle_recipient(self, client: TestClient) -> None:
        create_resp = client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        rid = create_resp.json()["id"]
        resp = client.patch(f"/api/recipients/{rid}/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        resp = client.patch(f"/api/recipients/{rid}/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_batch_toggle_enable(self, client: TestClient) -> None:
        client.post("/api/recipients", json={"email": "a@example.com"})
        client.post("/api/recipients", json={"email": "b@example.com"})
        resp = client.post("/api/recipients/batch-toggle", json={"enabled": True})
        assert resp.status_code == 200

    def test_batch_toggle_disable(self, client: TestClient) -> None:
        client.post("/api/recipients", json={"email": "a@example.com"})
        resp = client.post("/api/recipients/batch-toggle", json={"enabled": False})
        assert resp.status_code == 200

    def test_update_recipient(self, client: TestClient) -> None:
        create_resp = client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        rid = create_resp.json()["id"]
        resp = client.patch(f"/api/recipients/{rid}", json={"email": "updated@example.com", "note": "新备注"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "updated@example.com"
        assert data["note"] == "新备注"

    def test_delete_recipient(self, client: TestClient) -> None:
        create_resp = client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        rid = create_resp.json()["id"]
        resp = client.delete(f"/api/recipients/{rid}")
        assert resp.status_code == 200

    def test_delete_nonexistent_recipient(self, client: TestClient) -> None:
        resp = client.delete("/api/recipients/99999")
        assert resp.status_code == 404

    def test_batch_create_recipients(self, client: TestClient) -> None:
        payload = [
            {"email": "batch1@example.com", "note": "批量1"},
            {"email": "batch2@example.com", "note": "批量2"},
            {"email": "batch3@example.com", "note": ""},
        ]
        resp = client.post("/api/recipients/batch", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert data[0]["email"] == "batch1@example.com"
        assert data[1]["email"] == "batch2@example.com"

    def test_create_recipient_empty_email(self, client: TestClient) -> None:
        resp = client.post("/api/recipients", json={"email": ""})
        assert resp.status_code == 422


# ── 预览分配 ──────────────────────────────────────────────────────────────


class TestPreview:
    def test_preview_assignments(self, client: TestClient) -> None:
        payload = {
            "content": {"subject": "测试", "body": "内容"},
            "sender_ids": [saved_sender(client, "s1@example.com", enabled=True)],
            "recipients": [{"email": "r1@example.com"}, {"email": "r2@example.com"}, {"email": "r3@example.com"}],
        }
        resp = client.post("/api/assignments/preview", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        for item in data:
            assert item["status"] == "pending"

    def test_preview_no_recipients(self, client: TestClient) -> None:
        payload = {
            "content": {"subject": "测试", "body": "内容"},
            "sender_ids": [saved_sender(client, "s1@example.com", enabled=True)],
            "recipients": [],
        }
        resp = client.post("/api/assignments/preview", json=payload)
        assert resp.status_code == 422

    def test_preview_no_enabled_sender(self, client: TestClient) -> None:
        payload = {
            "content": {"subject": "测试", "body": "内容"},
            "sender_ids": [saved_sender(client, "s1@example.com", enabled=False)],
            "recipients": [{"email": "r1@example.com"}],
        }
        resp = client.post("/api/assignments/preview", json=payload)
        assert resp.status_code == 409


# ── 批量导入后使用模板群发的回归测试 ──────────────────────────────────────


class TestBatchTemplateSending:
    def test_150_recipients_with_template_and_attachment(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import main

        # 只检查任务创建；不连接 SMTP，不发送真实邮件。
        monkeypatch.setattr(main, "run_task", AsyncMock())
        monkeypatch.setattr(main, "TASKS", {})
        recipients = [{"email": f"user{i}@example.com"} for i in range(150)]
        imported = client.post("/api/recipients/batch", json=recipients)
        assert imported.status_code == 200
        assert len(imported.json()) == 150

        saved = client.post("/api/saved-mails", data=MAIL_PAYLOAD, files={
            "attachments": ("sample.txt", b"template attachment", "text/plain"),
        })
        assert saved.status_code == 200
        template = client.get(f"/api/saved-mails/{saved.json()['id']}").json()
        attachment = client.get(f"/api/saved-mails/{template['id']}/attachments/{template['attachments'][0]}")
        assert attachment.status_code == 200
        payload = {
            "content": {"subject": template["subject"], "body": template["body"]},
            "sender_ids": [saved_sender(client, "sender@example.com", enabled=True)],
            "recipients": [{"email": r["email"]} for r in imported.json()],
        }
        result = client.post("/api/tasks", data={"payload": json.dumps(payload)}, files={
            "attachments": ("sample.txt", attachment.content, "text/plain"),
        })
        assert result.status_code == 200
        task = result.json()
        assert task["total"] == 150
        assert {a["recipient"] for a in task["assignments"]} == {r["email"] for r in recipients}
        assert main.TASKS[task["task_id"]].attachments[0].content == b"template attachment"

    @pytest.mark.parametrize("invalid_email", ["bad..address@example.com", "user@example.com.", "user@bad_domain.com", "user\u2060@example.com"])
    def test_invalid_address_rejects_entire_import_before_writing(self, client: TestClient, invalid_email: str) -> None:
        client.post("/api/recipients", json={"email": "existing@example.com"})
        recipients = [{"email": f"user{i}@example.com"} for i in range(149)] + [{"email": invalid_email}]
        result = client.post("/api/recipients/batch", json=recipients)
        assert result.status_code == 422
        assert result.json()["detail"][0]["loc"] == ["body", 149, "email"]
        assert [r["email"] for r in client.get("/api/recipients").json()] == ["existing@example.com"]

    def test_existing_invalid_recipient_identified_when_sending(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        import main

        worker = AsyncMock()
        monkeypatch.setattr(main, "run_task", worker)
        monkeypatch.setattr(main, "TASKS", {})
        payload = {
            "content": MAIL_PAYLOAD,
            "sender_ids": [saved_sender(client, "sender@example.com", enabled=True)],
            "recipients": [{"email": f"user{i}@example.com"} for i in range(149)] + [{"email": "bad..address@example.com"}],
        }
        result = client.post("/api/tasks", data={"payload": json.dumps(payload)})
        assert result.status_code == 400
        error = result.json()["detail"][0]
        assert error["loc"] == ["recipients", 149, "email"]
        assert error["input"] == "bad..address@example.com"
        assert "secret-test-only" not in result.text
        assert not main.TASKS
        worker.assert_not_called()

    def test_other_task_validation_errors_keep_location_without_sensitive_input(self, client: TestClient) -> None:
        payload = {
            "content": {"subject": "x" * 201, "body": "private body"},
            "senders": [{"email": "sender@example.com", "auth_code": "secret-test-only", "enabled": False}],
            "recipients": [{"email": "user@example.com"}],
        }
        result = client.post("/api/tasks", data={"payload": json.dumps(payload)})
        assert result.status_code == 400
        assert ["content", "subject"] in [error["loc"] for error in result.json()["detail"]]
        assert ["senders"] in [error["loc"] for error in result.json()["detail"]]
        assert "secret-test-only" not in result.text
        assert "private body" not in result.text
        assert all("input" not in error for error in result.json()["detail"])

    def test_invalid_json_returns_validation_error(self, client: TestClient) -> None:
        result = client.post("/api/tasks", data={"payload": "{"})
        assert result.status_code == 400
        assert result.json()["detail"][0]["type"] == "json_invalid"

    def test_single_and_edit_recipient_use_same_validation(self, client: TestClient) -> None:
        assert client.post("/api/recipients", json={"email": "bad..address@example.com"}).status_code == 422
        created = client.post("/api/recipients", json={"email": " User@Example.com "}).json()
        assert created["email"] == "user@example.com"
        assert client.patch(f"/api/recipients/{created['id']}", json={"email": "user@example.com."}).status_code == 422
        assert client.get("/api/recipients").json()[0]["email"] == "user@example.com"
        assert client.patch(f"/api/recipients/{created['id']}", json={"note": "valid note"}).status_code == 200

    def test_sender_and_template_validated_before_saving(self, client: TestClient) -> None:
        assert client.post("/api/sender-configs", json={**SENDER_PAYLOAD, "email": "bad..sender@example.com"}).status_code == 422
        assert client.post("/api/saved-mails", data={"subject": "x" * 201, "body": "content"}).status_code == 422
        assert client.get("/api/saved-mails").json() == []

    @pytest.mark.parametrize("copied_email", ["\u200bUser@Example.com", "User@Example.com\u200b", "\ufeffUser@Example.com", "mailto:User@Example.com", " MAILTO:User@Example.com\u200b "])
    def test_copy_artifacts_normalized_for_import_edit_and_task(self, client: TestClient, monkeypatch: pytest.MonkeyPatch, copied_email: str) -> None:
        import main

        monkeypatch.setattr(main, "run_task", AsyncMock())
        monkeypatch.setattr(main, "TASKS", {})
        imported = client.post("/api/recipients/batch", json=[{"email": copied_email}])
        assert imported.status_code == 200
        recipient = imported.json()[0]
        assert recipient["email"] == "user@example.com"
        edited = client.patch(f"/api/recipients/{recipient['id']}", json={"email": copied_email})
        assert edited.status_code == 200
        assert edited.json()["email"] == "user@example.com"
        payload = {"content": MAIL_PAYLOAD, "sender_ids": [saved_sender(client, "sender@example.com", enabled=True)], "recipients": [{"email": copied_email}, {"email": "user@example.com"}]}
        result = client.post("/api/tasks", data={"payload": json.dumps(payload)})
        assert result.status_code == 200
        assert result.json()["total"] == 1
        assert result.json()["assignments"][0]["recipient"] == "user@example.com"

    def test_success_disables_legacy_copied_addresses_and_duplicates(self, client: TestClient) -> None:
        import main

        # Emulate existing rows from the old import endpoint, bypassing current validation.
        originals = ["user@example.com\u200b", "mailto:User@example.com", "user@example.com", "other@example.com"]
        with main.db() as conn:
            conn.executemany("INSERT INTO recipients (email, enabled, note, created_at) VALUES (?,1,'keep note','2026-01-01')", [(email,) for email in originals])
            conn.commit()
        main.disable_recipient("user@example.com")
        records = client.get("/api/recipients").json()
        assert all(not r["enabled"] for r in records if r["email"] != "other@example.com")
        assert next(r for r in records if r["email"] == "other@example.com")["enabled"]
        assert all(r["note"] == "keep note" for r in records)
        assert {r["email"] for r in records} == set(originals)


# ── 保存邮件 ──────────────────────────────────────────────────────────────


class TestSavedMails:
    def test_save_mail(self, client: TestClient) -> None:
        resp = client.post("/api/saved-mails", data=MAIL_PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["subject"] == MAIL_PAYLOAD["subject"]
        assert data["body"] == MAIL_PAYLOAD["body"]
        assert "id" in data

    def test_list_saved_mails(self, client: TestClient) -> None:
        client.post("/api/saved-mails", data=MAIL_PAYLOAD)
        resp = client.get("/api/saved-mails")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_get_saved_mail(self, client: TestClient) -> None:
        create_resp = client.post("/api/saved-mails", data=MAIL_PAYLOAD)
        mid = create_resp.json()["id"]
        resp = client.get(f"/api/saved-mails/{mid}")
        assert resp.status_code == 200
        assert resp.json()["subject"] == MAIL_PAYLOAD["subject"]

    def test_delete_saved_mail(self, client: TestClient) -> None:
        create_resp = client.post("/api/saved-mails", data=MAIL_PAYLOAD)
        mid = create_resp.json()["id"]
        resp = client.delete(f"/api/saved-mails/{mid}")
        assert resp.status_code == 200

    def test_get_nonexistent_saved_mail(self, client: TestClient) -> None:
        resp = client.get("/api/saved-mails/99999")
        assert resp.status_code == 404

    def test_delete_nonexistent_saved_mail(self, client: TestClient) -> None:
        resp = client.delete("/api/saved-mails/99999")
        assert resp.status_code == 404


# ── 统计 ──────────────────────────────────────────────────────────────────


class TestStats:
    def test_stats_hour(self, client: TestClient) -> None:
        resp = client.get("/api/stats?period=hour")
        assert resp.status_code == 200
        data = resp.json()
        assert "stats" in data
        assert isinstance(data["total_success"], int)
        assert isinstance(data["total_failed"], int)
        assert isinstance(data["success_rate"], float)

    def test_stats_day(self, client: TestClient) -> None:
        resp = client.get("/api/stats?period=day")
        assert resp.status_code == 200

    def test_stats_week(self, client: TestClient) -> None:
        resp = client.get("/api/stats?period=week")
        assert resp.status_code == 200

    def test_stats_month(self, client: TestClient) -> None:
        resp = client.get("/api/stats?period=month")
        assert resp.status_code == 200

    def test_stats_invalid_period(self, client: TestClient) -> None:
        # 后端不校验 period 参数，非法值默认返回 hour 数据
        resp = client.get("/api/stats?period=invalid")
        assert resp.status_code == 200

    def test_sender_breakdown(self, client: TestClient) -> None:
        resp = client.get("/api/stats/sender-breakdown")
        assert resp.status_code == 200

    def test_recipient_breakdown(self, client: TestClient) -> None:
        resp = client.get("/api/stats/recipient-breakdown")
        assert resp.status_code == 200

    def test_failure_reasons(self, client: TestClient) -> None:
        resp = client.get("/api/stats/failure-reasons")
        assert resp.status_code == 200

    def test_failed_recipients(self, client: TestClient) -> None:
        resp = client.get("/api/stats/failed-recipients")
        assert resp.status_code == 200

    def test_failed_recipients_total(self, client: TestClient) -> None:
        resp = client.get("/api/stats/failed-recipients/total")
        assert resp.status_code == 200

    def test_failed_recipients_emails(self, client: TestClient) -> None:
        resp = client.get("/api/stats/failed-recipients/emails")
        assert resp.status_code == 200

    def test_failed_recipients_pagination(self, client: TestClient) -> None:
        resp = client.get("/api/stats/failed-recipients?page=1&page_size=5")
        assert resp.status_code == 200


# ── 完整业务流程测试 ───────────────────────────────────────────────────────


class TestWorkflow:
    """模拟一个完整的群发任务流程"""

    def test_full_workflow(self, client: TestClient) -> None:
        # 1. 健康检查
        assert client.get("/api/health").status_code == 200

        # 2. 创建发送邮箱
        r1 = client.post("/api/sender-configs", json={
            "email": "sender1@example.com", "auth_code": "test1234567890", "note": "主邮箱",
        })
        assert r1.status_code == 200

        # 3. 创建收件人
        r2 = client.post("/api/recipients", json={"email": "user1@example.com"})
        assert r2.status_code == 200
        r3 = client.post("/api/recipients", json={"email": "user2@example.com"})
        assert r3.status_code == 200
        r4 = client.post("/api/recipients", json={"email": "user3@example.com"})
        assert r4.status_code == 200

        # 4. 切换收件人状态
        rid = r2.json()["id"]
        client.patch(f"/api/recipients/{rid}/toggle")
        r5 = client.patch(f"/api/recipients/{rid}/toggle")
        assert r5.json()["enabled"] is True

        # 5. 获取收件人列表
        r6 = client.get("/api/recipients")
        assert len(r6.json()) >= 3

        # 6. 预览分配
        r7 = client.post("/api/assignments/preview", json={
            "content": {"subject": "群发测试", "body": "测试正文"},
            "sender_ids": [saved_sender(client, "sender1@example.com", enabled=True)],
            "recipients": [{"email": "user1@example.com"}, {"email": "user2@example.com"}, {"email": "user3@example.com"}],
        })
        assert r7.status_code == 200
        assert len(r7.json()) == 3

        # 7. 保存邮件
        r8 = client.post("/api/saved-mails", data={"subject": "群发测试", "body": "测试正文"})
        assert r8.status_code == 200
        mail_id = r8.json()["id"]

        # 8. 获取保存的邮件
        r9 = client.get(f"/api/saved-mails/{mail_id}")
        assert r9.status_code == 200

        # 9. 获取所有保存的邮件
        r10 = client.get("/api/saved-mails")
        assert len(r10.json()) >= 1

        # 10. 请求统计
        assert client.get("/api/stats?period=hour").status_code == 200
        assert client.get("/api/stats/sender-breakdown").status_code == 200
        assert client.get("/api/stats/recipient-breakdown").status_code == 200

        # 11. 批量启用/禁用收件人
        assert client.post("/api/recipients/batch-toggle", json={"enabled": False}).status_code == 200
        assert client.post("/api/recipients/batch-toggle", json={"enabled": True}).status_code == 200

        # 12. 查询失败邮箱（此时应该为空）
        assert client.get("/api/stats/failed-recipients").status_code == 200
        assert client.get("/api/stats/failed-recipients/total").json()["total"] == 0
        assert client.get("/api/stats/failed-recipients/emails").json() == []
