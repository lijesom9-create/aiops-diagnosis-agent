"""
管理后台权限测试（专题1：RBAC 收紧）

验证写操作收紧到 admin 角色：
1. 普通用户/教师上传文档 → 403
2. 普通用户删除文档 → 403
3. 未认证上传 → 401
4. admin 上传通过权限层（不被 403 拒绝）
5. SUPER_ADMIN_USERNAME 注册自动获得 admin 角色

说明：权限拒绝在依赖注入层完成（require_admin），不会进入函数体，
因此不依赖 knowledge_store 初始化，测试轻量可靠。
"""

import pytest
import uuid
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from main import app


@pytest.fixture(scope="function")
def client():
    """每个测试独立的 TestClient"""
    with TestClient(app) as c:
        yield c


def _register(client, role="student", username=None):
    """注册用户并返回 token"""
    username = username or f"u_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "email": f"{username}@test.com",
        "role": role,
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestWriteOpsRequireAdmin:
    """写操作（上传/删除）必须 admin 角色"""

    def test_student_upload_forbidden(self, client):
        """普通用户上传文档 → 403"""
        token = _register(client, role="student")
        resp = client.post(
            "/api/documents/upload",
            headers=auth_header(token),
            files={"file": ("test.txt", b"hello", "text/plain")},
            data={"title": "test"},
        )
        assert resp.status_code == 403, resp.text

    def test_teacher_upload_forbidden(self, client):
        """教师上传文档 → 403（教师无管理后台权限）"""
        token = _register(client, role="teacher")
        resp = client.post(
            "/api/documents/upload",
            headers=auth_header(token),
            files={"file": ("test.txt", b"hello", "text/plain")},
            data={"title": "test"},
        )
        assert resp.status_code == 403, resp.text

    def test_unauthenticated_upload_unauthorized(self, client):
        """未认证上传 → 401"""
        resp = client.post(
            "/api/documents/upload",
            files={"file": ("test.txt", b"hello", "text/plain")},
            data={"title": "test"},
        )
        assert resp.status_code == 401, resp.text

    def test_student_delete_forbidden(self, client):
        """普通用户删除文档 → 403（在权限层拒绝，不查文档存在性）"""
        token = _register(client, role="student")
        resp = client.delete(
            "/api/documents/doc_nonexistent",
            headers=auth_header(token),
        )
        assert resp.status_code == 403, resp.text

    def test_student_batch_upload_forbidden(self, client):
        """普通用户批量上传 → 403"""
        token = _register(client, role="student")
        resp = client.post(
            "/api/documents/batch-upload",
            headers=auth_header(token),
            files=[("files", ("a.txt", b"hello", "text/plain"))],
        )
        assert resp.status_code == 403, resp.text

    def test_admin_upload_passes_auth(self, client):
        """admin 上传通过权限层（不被 403 拒绝）

        用不支持的文件类型触发 400，证明已通过 require_admin 进入函数体。
        """
        token = _register(client, role="admin")
        resp = client.post(
            "/api/documents/upload",
            headers=auth_header(token),
            files={"file": ("bad.unsupportedext", b"hello", "application/octet-stream")},
            data={"title": "test"},
        )
        # admin 通过权限校验，但文件类型不支持 → 400（不是 403）
        assert resp.status_code == 400, resp.text


class TestSuperAdminBootstrap:
    """SUPER_ADMIN_USERNAME 自动初始化 admin 角色"""

    def test_super_admin_username_gets_admin_role(self, client, monkeypatch):
        """注册用户名匹配 SUPER_ADMIN_USERNAME → 自动 admin（即使填 student）"""
        from app.core.config import settings
        super_name = f"super_{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(settings, "SUPER_ADMIN_USERNAME", super_name)

        resp = client.post("/api/auth/register", json={
            "username": super_name,
            "password": "test123456",
            "email": f"{super_name}@test.com",
            "role": "student",  # 即使填 student，匹配超管名也会被提升
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]

        me = client.get("/api/auth/me", headers=auth_header(token))
        assert me.status_code == 200
        assert me.json()["role"] == "admin"

    def test_non_super_admin_username_stays_student(self, client, monkeypatch):
        """用户名不匹配 SUPER_ADMIN_USERNAME → 保持申请时的角色"""
        from app.core.config import settings
        monkeypatch.setattr(settings, "SUPER_ADMIN_USERNAME", "someone_else")

        token = _register(client, role="student")
        me = client.get("/api/auth/me", headers=auth_header(token))
        assert me.status_code == 200
        assert me.json()["role"] == "student"


class TestRetryApi:
    """重试 API（专题2：Celery 异步导入）"""

    def test_retry_requires_celery(self, client):
        """USE_CELERY=False 时重试 API 返回 400（降级模式不支持重试）"""
        token = _register(client, role="admin")
        resp = client.post(
            "/api/documents/doc_nonexistent/retry",
            headers=auth_header(token),
        )
        assert resp.status_code == 400
        assert "Celery" in resp.json()["detail"] or "USE_CELERY" in resp.json()["detail"]

    def test_student_retry_forbidden(self, client):
        """普通用户重试 → 403（权限层先拒绝，不检查 USE_CELERY）"""
        token = _register(client, role="student")
        resp = client.post(
            "/api/documents/doc_nonexistent/retry",
            headers=auth_header(token),
        )
        assert resp.status_code == 403
