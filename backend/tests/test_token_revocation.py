"""
C1 JWT 服务端吊销测试（token_version 版本号方案）

覆盖：
1. logout 后旧 token 立即失效（401）——即使 cookie 未清/被盗用
2. logout 后重新登录获得新 token，新 token 有效
3. 管理员降权用户角色后，该用户旧 token 立即失效（防降权后仍持 admin 权限）
4. 正常 token 多次请求持续有效（无误伤）

用唯一用户名注册避免真实 Mongo 残留撞键。
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(scope="function")
def client():
    with TestClient(app) as c:
        yield c


def _register(client, username=None):
    username = username or f"u_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "email": f"{username}@test.com",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"], username


def _login(client, username):
    resp = client.post("/api/auth/login", json={
        "username": username,
        "password": "test123456",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTokenRevocation:
    """C1：JWT 服务端吊销"""

    def test_token_valid_before_logout(self, client):
        """正常 token 多次请求持续有效（无误伤）"""
        token, _ = _register(client)
        for _ in range(3):
            me = client.get("/api/auth/me", headers=auth_header(token))
            assert me.status_code == 200, me.text

    def test_logout_invalidates_old_token(self, client):
        """logout 后旧 token 立即失效（401）"""
        token, username = _register(client)

        # 确认 token 有效
        me = client.get("/api/auth/me", headers=auth_header(token))
        assert me.status_code == 200

        # 登出（递增 token_version）
        resp = client.post("/api/auth/logout", headers=auth_header(token))
        assert resp.status_code == 200

        # 旧 token 现在应失效
        me2 = client.get("/api/auth/me", headers=auth_header(token))
        assert me2.status_code == 401, f"logout 后旧 token 应失效: {me2.text}"

    def test_relogin_after_logout_works(self, client):
        """logout 后重新登录获得新 token，新 token 有效"""
        token, username = _register(client)
        client.post("/api/auth/logout", headers=auth_header(token))

        # 旧 token 失效
        assert client.get("/api/auth/me", headers=auth_header(token)).status_code == 401

        # 重新登录
        new_token = _login(client, username)
        me = client.get("/api/auth/me", headers=auth_header(new_token))
        assert me.status_code == 200
        assert me.json()["username"] == username

    def test_role_change_invalidates_old_token(self, client):
        """管理员降权用户角色后，该用户旧 token 立即失效（防降权后仍持 admin 权限）"""
        from app.core.config import settings

        # 注册一个 admin（超管用户名）
        super_name = f"super_{uuid.uuid4().hex[:8]}"
        prev = settings.SUPER_ADMIN_USERNAME
        settings.SUPER_ADMIN_USERNAME = super_name
        admin_token, _ = _register(client, username=super_name)
        settings.SUPER_ADMIN_USERNAME = prev

        # 注册一个普通 student（注意：register 会设 cookie，后续用 header 鉴权前需清 cookie
        # 否则 get_current_user 优先读 cookie 而非 Authorization 头）
        student_token, student_username = _register(client)
        client.cookies.clear()

        # 确认 student token 有效（用 Authorization 头）
        me = client.get("/api/auth/me", headers=auth_header(student_token))
        assert me.status_code == 200
        student_user_id = me.json()["user_id"]

        # admin 把 student 提升为 teacher（角色变更触发 token_version 递增）
        resp = client.patch(
            "/api/admin/users/{}/role".format(student_user_id),
            json={"role": "teacher"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text

        # student 旧 token 应失效
        me2 = client.get("/api/auth/me", headers=auth_header(student_token))
        assert me2.status_code == 401, "角色变更后旧 token 应失效"

        # 重新登录后新 token 带 teacher 角色
        new_student_token = _login(client, student_username)
        client.cookies.clear()
        me3 = client.get("/api/auth/me", headers=auth_header(new_student_token)).json()
        assert me3["role"] == "teacher"
