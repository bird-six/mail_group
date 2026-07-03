from __future__ import annotations

import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

# 确保能找到 backend 目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import init_db


@pytest.fixture(autouse=True)
def _test_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """使用临时数据库，测试之间互不干扰"""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "test.sqlite3")
    monkeypatch.setattr("main.DB_PATH", db_path)
    monkeypatch.setattr("main.DATA_DIR", tmp)
    monkeypatch.setattr("main.SAVED_ATTACHMENTS_DIR", os.path.join(tmp, "saved_attachments"))
    # 重置全局连接
    import main as main_module
    main_module._shared_conn = None
    init_db()
    yield


@pytest.fixture
def client(_test_db: None) -> TestClient:
    from main import app
    return TestClient(app)


SENDER_PAYLOAD = {
    "email": "test_sender@qq.com",
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
        assert data["auth_code"] == SENDER_PAYLOAD["auth_code"]
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
        resp = client.post("/api/sender-configs", json={"email": "empty@qq.com", "auth_code": ""})
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
        client.post("/api/recipients", json={"email": "a@test.com"})
        client.post("/api/recipients", json={"email": "b@test.com"})
        resp = client.post("/api/recipients/batch-toggle", json={"enabled": True})
        assert resp.status_code == 200

    def test_batch_toggle_disable(self, client: TestClient) -> None:
        client.post("/api/recipients", json={"email": "a@test.com"})
        resp = client.post("/api/recipients/batch-toggle", json={"enabled": False})
        assert resp.status_code == 200

    def test_update_recipient(self, client: TestClient) -> None:
        create_resp = client.post("/api/recipients", json=RECIPIENT_PAYLOAD)
        rid = create_resp.json()["id"]
        resp = client.patch(f"/api/recipients/{rid}", json={"email": "updated@test.com", "note": "新备注"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "updated@test.com"
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
            {"email": "batch1@test.com", "note": "批量1"},
            {"email": "batch2@test.com", "note": "批量2"},
            {"email": "batch3@test.com", "note": ""},
        ]
        resp = client.post("/api/recipients/batch", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert data[0]["email"] == "batch1@test.com"
        assert data[1]["email"] == "batch2@test.com"

    def test_create_recipient_empty_email(self, client: TestClient) -> None:
        resp = client.post("/api/recipients", json={"email": ""})
        assert resp.status_code == 422


# ── 预览分配 ──────────────────────────────────────────────────────────────


class TestPreview:
    def test_preview_assignments(self, client: TestClient) -> None:
        payload = {
            "content": {"subject": "测试", "body": "内容"},
            "senders": [{"email": "s1@qq.com", "auth_code": "1234567890123456"}],
            "recipients": [{"email": "r1@test.com"}, {"email": "r2@test.com"}, {"email": "r3@test.com"}],
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
            "senders": [{"email": "s1@qq.com", "auth_code": "1234567890123456"}],
            "recipients": [],
        }
        resp = client.post("/api/assignments/preview", json=payload)
        assert resp.status_code == 422

    def test_preview_no_enabled_sender(self, client: TestClient) -> None:
        payload = {
            "content": {"subject": "测试", "body": "内容"},
            "senders": [{"email": "s1@qq.com", "auth_code": "1234567890123456", "enabled": False}],
            "recipients": [{"email": "r1@test.com"}],
        }
        resp = client.post("/api/assignments/preview", json=payload)
        assert resp.status_code == 422


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
            "email": "sender1@qq.com", "auth_code": "test1234567890", "note": "主邮箱",
        })
        assert r1.status_code == 200

        # 3. 创建收件人
        r2 = client.post("/api/recipients", json={"email": "user1@test.com"})
        assert r2.status_code == 200
        r3 = client.post("/api/recipients", json={"email": "user2@test.com"})
        assert r3.status_code == 200
        r4 = client.post("/api/recipients", json={"email": "user3@test.com"})
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
            "senders": [{"email": "sender1@qq.com", "auth_code": "test1234567890"}],
            "recipients": [{"email": "user1@test.com"}, {"email": "user2@test.com"}, {"email": "user3@test.com"}],
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
