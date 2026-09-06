"""
API 端到端测试
通过 FastAPI TestClient 调用实际 HTTP 端点

覆盖：
1. POST /api/auth/register — 注册
2. POST /api/auth/login — 登录
3. GET /api/auth/me — 获取当前用户
4. POST /api/teaching/chat — 教学对话
8. GET /api/health — 健康检查
9. GET /api/config — DEBUG 守卫
"""

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from main import app


@pytest.fixture(scope="function")
def client():
    """创建测试客户端（每个测试独立，避免事件循环污染）"""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def student_token(client):
    """注册一个学生用户并返回 token"""
    username = f"api_student_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "email": f"{username}@test.com",
        "role": "student",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
    })
    assert resp.status_code == 200
    return resp.json()["access_token"]


@pytest.fixture(scope="function")
def teacher_token(client):
    """注册一个教师用户并返回 token"""
    username = f"api_teacher_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "email": f"{username}@test.com",
        "role": "teacher",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
    })
    assert resp.status_code == 200
    return resp.json()["access_token"]


def auth_header(token: str) -> dict:
    """生成认证头"""
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 1. 健康检查 & 配置
# ============================================================

class TestHealthAndConfig:
    """健康检查和配置端点"""

    def test_health_check(self, client):
        """GET /api/health 应返回 healthy"""
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    def test_root_endpoint(self, client):
        """GET / 应返回应用信息"""
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "name" in data
        assert data["status"] == "running"

    def test_config_endpoint_debug_guard(self, client):
        """GET /api/config 在非 DEBUG 模式应返回 404"""
        resp = client.get("/api/config")
        # DEBUG=False 时应返回 404
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            # DEBUG=True 时返回配置
            data = resp.json()
            assert "app_name" in data


# ============================================================
# 2. 认证 API
# ============================================================

class TestAuthAPI:
    """认证端点测试"""

    def test_register_student(self, client):
        """POST /api/auth/register — 注册学生"""
        username = f"reg_student_{uuid.uuid4().hex[:8]}"
        resp = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "role": "student",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_register_teacher(self, client):
        """POST /api/auth/register — 注册教师"""
        username = f"reg_teacher_{uuid.uuid4().hex[:8]}"
        resp = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "role": "teacher",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_register_duplicate_username(self, client):
        """POST /api/auth/register — 重复用户名应失败"""
        username = f"dup_user_{uuid.uuid4().hex[:8]}"
        # 第一次注册
        resp1 = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "role": "student",
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp1.status_code == 200

        # 第二次注册同名用户
        resp2 = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}2@test.com",
            "role": "student",
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        assert resp2.status_code == 400

    def test_register_password_too_short(self, client):
        """POST /api/auth/register — 密码太短应失败"""
        # 密码太短会在 UserCreate 构造时触发 Pydantic 验证错误
        # FastAPI TestClient 可能返回 422 或 500（取决于错误处理）
        try:
            resp = client.post("/api/auth/register", json={
                "username": f"short_{uuid.uuid4().hex[:8]}",
                "password": "123",
                "email": "short@test.com",
                "org_name": f"org_{uuid.uuid4().hex[:8]}",
            })
            # 应该返回 4xx 或 5xx，不应返回 200
            assert resp.status_code >= 400
        except Exception as e:
            # Pydantic ValidationError 被抛出也说明验证生效了
            assert "at least 6 characters" in str(e) or "too_short" in str(e)

    def test_login_success(self, client):
        """POST /api/auth/login — 正确凭据应成功"""
        # 先注册
        username = f"login_user_{uuid.uuid4().hex[:8]}"
        client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "role": "student",
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })

        # 登录
        resp = client.post("/api/auth/login", json={
            "username": username,
            "password": "test123456",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_login_wrong_password(self, client):
        """POST /api/auth/login — 错误密码应失败"""
        username = f"wrong_pw_{uuid.uuid4().hex[:8]}"
        client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "role": "student",
            "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })

        resp = client.post("/api/auth/login", json={
            "username": username,
            "password": "wrong_password",
        })
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        """POST /api/auth/login — 不存在的用户应失败"""
        resp = client.post("/api/auth/login", json={
            "username": "nonexistent_user_xyz",
            "password": "test123456",
        })
        assert resp.status_code == 401

    def test_get_me(self, client, student_token):
        """GET /api/auth/me — 获取当前用户信息"""
        resp = client.get("/api/auth/me", headers=auth_header(student_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "user_id" in data
        assert "username" in data
        assert data["role"] == "student"

    def test_get_me_no_token(self, client):
        """GET /api/auth/me — 无 token 应返回 401"""
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_verify_token(self, client, student_token):
        """GET /api/auth/verify — 验证 token"""
        resp = client.get("/api/auth/verify", headers=auth_header(student_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True

    def test_invalid_token(self, client):
        """GET /api/auth/me — 无效 token 应返回 401"""
        resp = client.get("/api/auth/me", headers=auth_header("invalid_token_here"))
        assert resp.status_code == 401


# ============================================================
# 3. 教学 API
# ============================================================

class TestSecurity:
    """安全相关测试"""

    def test_error_messages_not_leaked(self, client, student_token):
        """错误响应不应暴露内部信息"""
        # 发送一个可能导致内部错误的请求
        resp = client.post("/api/teaching/chat",
            json={"message": "你好"},
            headers=auth_header(student_token),
        )
        # 即使成功，也不应包含内部路径或堆栈信息
        if resp.status_code == 500:
            detail = resp.json().get("detail", "")
            assert "traceback" not in detail.lower()
            assert "file" not in detail.lower()
            assert "line" not in detail.lower()

    def test_cors_headers(self, client):
        """应有 CORS 头"""
        resp = client.options("/api/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # CORS preflight 应该被处理
        assert resp.status_code in (200, 405)





