"""
安全加固回归测试

覆盖三类历史漏洞的修复：
1. 知识库管理端点越权（任意登录用户可读/删向量数据）→ 仅 admin
2. 告警 webhook 无鉴权（任何人可伪造告警/刷飞书配额）→ 共享密钥验证
3. 登录/注册无限流（可暴力破解）→ 按 IP 限流

说明：权限拒绝在依赖注入层完成（require_admin / 密钥校验在函数体最前），
不依赖 knowledge_store / feishu 初始化，测试轻量可靠。
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient
from test_admin_api import _register, auth_header

from app.api.alerts import reset_feishu_client
from app.core.config import settings
from app.core.rate_limiter import reset_auth_rate_limiter
from main import app


@pytest.fixture(scope="function")
def client():
    with TestClient(app) as c:
        yield c


class TestKnowledgeAdminOnly:
    """知识库管理端点仅 admin 可访问（chunk 内容/删除为高危操作）"""

    def test_student_get_chunks_forbidden(self, client):
        """普通用户读取文档 chunk → 403（chunk 含内容与元数据）"""
        token = _register(client)
        resp = client.get(
            "/api/knowledge/documents/doc_nonexistent",
            headers=auth_header(token),
        )
        assert resp.status_code == 403, resp.text

    def test_student_delete_chunks_forbidden(self, client):
        """普通用户删除文档向量数据 → 403"""
        token = _register(client)
        resp = client.delete(
            "/api/knowledge/documents/doc_nonexistent",
            headers=auth_header(token),
        )
        assert resp.status_code == 403, resp.text

    def test_student_stats_forbidden(self, client):
        """普通用户获取知识库统计 → 403（含全部文档名与规模信息）"""
        token = _register(client)
        resp = client.get("/api/knowledge/stats", headers=auth_header(token))
        assert resp.status_code == 403, resp.text

    def test_unauthenticated_delete_unauthorized(self, client):
        """未认证删除向量数据 → 401"""
        resp = client.delete("/api/knowledge/documents/doc_nonexistent")
        assert resp.status_code == 401, resp.text


class TestAlertWebhookSecret:
    """告警 webhook 共享密钥验证（防伪造告警/刷飞书配额）"""

    @pytest.fixture(autouse=True)
    def _clean(self):
        """每个测试后重置飞书客户端单例，避免跨测试污染"""
        reset_feishu_client()
        yield
        reset_feishu_client()

    @staticmethod
    def _payload() -> dict:
        return {
            "version": "4",
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {"alertname": "TestAlert", "severity": "warning"},
                "annotations": {"summary": "s"},
                "startsAt": "2026-08-07T03:00:00Z",
                "fingerprint": "fp-1",
            }],
        }

    def test_webhook_rejected_when_secret_unconfigured(self, client, monkeypatch):
        """服务端未配置 ALERT_WEBHOOK_SECRET → 503 拒绝处理（secure by default）"""
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_SECRET", None)
        resp = client.post("/api/alerts/webhook", json=self._payload())
        assert resp.status_code == 503, resp.text

    def test_webhook_rejects_wrong_secret(self, client, monkeypatch):
        """密钥错误 → 401"""
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_SECRET", "right-secret")
        resp = client.post(
            "/api/alerts/webhook",
            json=self._payload(),
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 401, resp.text

    def test_webhook_rejects_missing_secret(self, client, monkeypatch):
        """配置了密钥但请求未携带 → 401"""
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_SECRET", "right-secret")
        resp = client.post("/api/alerts/webhook", json=self._payload())
        assert resp.status_code == 401, resp.text

    def test_webhook_accepts_bearer_secret(self, client, monkeypatch):
        """正确的 Bearer 密钥通过鉴权（飞书未配置 → 503，证明已过密钥层）"""
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_SECRET", "right-secret")
        monkeypatch.setattr(settings, "FEISHU_APP_ID", None)
        monkeypatch.setattr(settings, "FEISHU_APP_SECRET", None)
        resp = client.post(
            "/api/alerts/webhook",
            json=self._payload(),
            headers={"Authorization": "Bearer right-secret"},
        )
        # 密钥正确 → 进入飞书配置检查 → 未配置返回 503（若密钥层失败会是 401）
        assert resp.status_code == 503, resp.text

    def test_webhook_accepts_custom_header_secret(self, client, monkeypatch):
        """自定义头 X-Webhook-Secret 同样可用"""
        monkeypatch.setattr(settings, "ALERT_WEBHOOK_SECRET", "right-secret")
        monkeypatch.setattr(settings, "FEISHU_APP_ID", None)
        monkeypatch.setattr(settings, "FEISHU_APP_SECRET", None)
        resp = client.post(
            "/api/alerts/webhook",
            json=self._payload(),
            headers={"X-Webhook-Secret": "right-secret"},
        )
        assert resp.status_code == 503, resp.text

    def test_alert_test_endpoint_requires_admin(self, client):
        """手动触发测试告警 → 普通用户 403"""
        token = _register(client)
        resp = client.get("/api/alerts/test", headers=auth_header(token))
        assert resp.status_code == 403, resp.text

    def test_alert_test_endpoint_admin_passes_auth(self, client, monkeypatch):
        """admin 通过权限层（飞书未配置 → 503，证明已过 require_admin）

        显式置空飞书配置：避免测试环境加载 .env 里的真实配置后真的外呼飞书 API。
        """
        monkeypatch.setattr(settings, "FEISHU_APP_ID", None)
        monkeypatch.setattr(settings, "FEISHU_APP_SECRET", None)
        token = _register(client, role="admin")
        resp = client.get("/api/alerts/test", headers=auth_header(token))
        assert resp.status_code == 503, resp.text


class TestAuthEndpointRateLimit:
    """登录/注册按 IP 限流（防暴力破解）"""

    @pytest.fixture(autouse=True)
    def _tight_limit(self, monkeypatch):
        """收紧阈值并重置限流器单例；测试结束后恢复，避免影响其他测试"""
        monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_PER_MIN", 3)
        reset_auth_rate_limiter()
        yield
        reset_auth_rate_limiter()

    def test_login_rate_limited_after_max_attempts(self, client):
        """连续失败登录超过阈值 → 429"""
        for _ in range(3):
            resp = client.post("/api/auth/login", json={
                "username": f"no_such_user_{uuid.uuid4().hex[:6]}",
                "password": "wrong",
            })
            assert resp.status_code == 401, resp.text

        resp = client.post("/api/auth/login", json={
            "username": "no_such_user_overflow",
            "password": "wrong",
        })
        assert resp.status_code == 429, resp.text
        assert "Retry-After" in resp.headers

    def test_register_rate_limited_after_max_attempts(self, client):
        """高频注册超过阈值 → 429（同 IP 批量注册被拦截）"""
        for _ in range(3):
            username = f"u_{uuid.uuid4().hex[:8]}"
            resp = client.post("/api/auth/register", json={
                "username": username,
                "password": "test123456",
                "email": f"{username}@test.com",
                "org_name": f"org_{uuid.uuid4().hex[:8]}",
            })
            assert resp.status_code == 200, resp.text

        username = "u_overflow"
        resp = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp.status_code == 429, resp.text
