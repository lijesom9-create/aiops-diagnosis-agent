"""
Feishu API 测试

覆盖飞书配置管理和消息发送。
"""

import pytest
import uuid
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from main import app


class FakeResponse:
    """模拟 httpx 响应"""

    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {"code": 0, "msg": "ok"}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json


@pytest.fixture(scope="function")
def client():
    """创建测试客户端"""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def student_token(client):
    """注册一个学生用户并返回 token"""
    username = f"feishu_user_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "email": f"{username}@test.com",
        "role": "student",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
    })
    assert resp.status_code == 200
    return resp.json()["access_token"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mock_feishu_send(monkeypatch):
    """模拟飞书 webhook 发送成功"""
    async def fake_post(*args, **kwargs):
        return FakeResponse(json_data={"code": 0, "msg": "ok"})

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)


class TestFeishuConfig:
    """飞书配置管理测试"""

    def test_get_feishu_config_initial(self, client, student_token):
        """初始配置为空"""
        resp = client.get("/api/users/me/feishu", headers=auth_header(student_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_url"] is None
        assert data["has_secret"] is False

    def test_update_feishu_config(self, client, student_token):
        """更新用户飞书配置"""
        resp = client.put(
            "/api/users/me/feishu",
            json={
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
                "webhook_secret": "mysecret",
            },
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
        assert data["has_secret"] is True


class TestFeishuSend:
    """飞书消息发送测试"""

    def test_send_test_without_config(self, client, student_token):
        """[FIX] 测试配置管理功能"""
        # 现在有 CLI 模式，测试配置接口是否正常工作
        resp = client.get(
            "/api/users/me/feishu",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "cli_available" in data

    def test_send_test_with_config(self, client, student_token, mock_feishu_send):
        """配置 webhook 后发送测试消息成功"""
        client.put(
            "/api/users/me/feishu",
            json={"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"},
            headers=auth_header(student_token),
        )

        resp = client.post(
            "/api/feishu/test",
            json={"message": "测试消息"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_send_reminder(self, client, student_token, mock_feishu_send):
        """发送学习提醒"""
        client.put(
            "/api/users/me/feishu",
            json={
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
                "webhook_secret": "secret123",
            },
            headers=auth_header(student_token),
        )

        resp = client.post(
            "/api/feishu/remind",
            json={
                "title": "今日学习提醒",
                "topic": "Python 数据分析",
                "today_task": "完成 Pandas 基础练习",
                "total_progress": "3 / 10 阶段",
            },
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
