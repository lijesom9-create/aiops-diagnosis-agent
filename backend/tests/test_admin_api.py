"""
管理后台 API 测试（专题3）

验证：
1. 普通用户访问 admin API → 403
2. admin 统计 API → 200
3. admin 用户管理（列表无敏感字段/角色变更/不能降级自己）
4. admin 任务监控 → 200
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from main import app


@pytest.fixture(scope="function")
def client():
    with TestClient(app) as c:
        yield c


def _register(client, role="student", username=None):
    """注册用户并返回 token

    role="admin" 时生成唯一用户名并临时将其设为 SUPER_ADMIN_USERNAME
    （用户名匹配自动获得 admin），注册后还原配置，避免污染其他测试。
    """
    if role == "admin" and username is None:
        username = f"super_{uuid.uuid4().hex[:8]}"
        from app.core.config import settings
        prev = settings.SUPER_ADMIN_USERNAME
        settings.SUPER_ADMIN_USERNAME = username
        try:
            return _do_register(client, username)
        finally:
            settings.SUPER_ADMIN_USERNAME = prev
    return _do_register(client, username or f"u_{uuid.uuid4().hex[:8]}")


def _do_register(client, username: str) -> str:
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "email": f"{username}@test.com",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_user_id(client, token: str) -> str:
    """从 /api/auth/me 获取 user_id"""
    me = client.get("/api/auth/me", headers=auth_header(token))
    assert me.status_code == 200
    return me.json()["user_id"]


class TestAdminApiAccess:
    """普通用户访问 admin API → 403（权限层拒绝）"""

    def test_student_stats_forbidden(self, client):
        token = _register(client, role="student")
        resp = client.get("/api/admin/stats", headers=auth_header(token))
        assert resp.status_code == 403

    def test_student_users_forbidden(self, client):
        token = _register(client, role="student")
        resp = client.get("/api/admin/users", headers=auth_header(token))
        assert resp.status_code == 403

    def test_student_tasks_forbidden(self, client):
        token = _register(client, role="student")
        resp = client.get("/api/admin/tasks", headers=auth_header(token))
        assert resp.status_code == 403

    def test_unauthenticated_stats_unauthorized(self, client):
        resp = client.get("/api/admin/stats")
        assert resp.status_code == 401


class TestAdminStats:
    """admin 统计 API"""

    def test_admin_stats_ok(self, client):
        token = _register(client, role="admin")
        resp = client.get("/api/admin/stats", headers=auth_header(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "documents" in data
        assert "users" in data
        assert "vectors" in data
        assert "total" in data["documents"]


class TestAdminUsers:
    """admin 用户管理"""

    def test_admin_list_users_no_sensitive_fields(self, client):
        """用户列表不含 hashed_password"""
        token = _register(client, role="admin")
        resp = client.get("/api/admin/users", headers=auth_header(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "users" in data
        assert "total" in data
        for u in data["users"]:
            assert "hashed_password" not in u, "用户列表不应包含 hashed_password"

    def test_admin_update_user_role(self, client):
        """admin 修改其他用户角色 student -> teacher

        C1 JWT 服务端吊销：角色变更会递增 token_version，使该用户旧 token 立即失效。
        因此角色变更后需重新登录拿新 token 才能验证新角色。
        """
        admin_token = _register(client, role="admin")
        student_username = f"u_{uuid.uuid4().hex[:8]}"
        student_token = _register(client, role="student", username=student_username)
        student_id = _get_user_id(client, student_token)

        # register/login 会设置 httpOnly cookie，且 get_current_user 优先读 cookie，
        # 因此最后一次 register（student）的 cookie 会覆盖 admin 身份。
        # 这里清除 cookie，强制走 Authorization 头（admin token）。
        client.cookies.clear()

        resp = client.patch(
            f"/api/admin/users/{student_id}/role",
            headers=auth_header(admin_token),
            json={"role": "teacher"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["new_role"] == "teacher"

        # C1：旧 student token 应已失效（角色变更触发 token_version 递增）
        client.cookies.clear()
        old_me = client.get("/api/auth/me", headers=auth_header(student_token))
        assert old_me.status_code == 401, "角色变更后旧 token 应被吊销（C1）"

        # 验证角色确实变更：重新登录拿新 token
        login_resp = client.post("/api/auth/login", json={
            "username": student_username,
            "password": "test123456",
        })
        assert login_resp.status_code == 200, login_resp.text
        new_token = login_resp.json()["access_token"]

        client.cookies.clear()
        me = client.get("/api/auth/me", headers=auth_header(new_token))
        assert me.status_code == 200, me.text
        assert me.json()["role"] == "teacher"

    def test_admin_cannot_demote_self(self, client):
        """admin 不能降级自己（防止误操作导致无管理员）"""
        admin_token = _register(client, role="admin")
        admin_id = _get_user_id(client, admin_token)

        resp = client.patch(
            f"/api/admin/users/{admin_id}/role",
            headers=auth_header(admin_token),
            json={"role": "student"},
        )
        assert resp.status_code == 400

    def test_admin_invalid_role_rejected(self, client):
        """非法角色 → 400"""
        admin_token = _register(client, role="admin")
        admin_id = _get_user_id(client, admin_token)

        resp = client.patch(
            f"/api/admin/users/{admin_id}/role",
            headers=auth_header(admin_token),
            json={"role": "superuser"},
        )
        assert resp.status_code == 400

    def test_admin_update_nonexistent_user(self, client):
        """修改不存在的用户 → 404"""
        admin_token = _register(client, role="admin")
        resp = client.patch(
            "/api/admin/users/nonexistent_id/role",
            headers=auth_header(admin_token),
            json={"role": "teacher"},
        )
        assert resp.status_code == 404


class TestAdminTasks:
    """admin 任务监控"""

    def test_admin_list_tasks_ok(self, client):
        token = _register(client, role="admin")
        resp = client.get("/api/admin/tasks", headers=auth_header(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "tasks" in data
        assert "total" in data

    def test_admin_list_tasks_with_filter(self, client):
        """按状态过滤任务"""
        token = _register(client, role="admin")
        resp = client.get(
            "/api/admin/tasks?status_filter=failed",
            headers=auth_header(token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # 所有返回的任务状态都应是 failed
        for t in data["tasks"]:
            assert t["status"] == "failed"
