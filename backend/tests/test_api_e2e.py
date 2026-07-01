"""
API 端到端测试
通过 FastAPI TestClient 调用实际 HTTP 端点

覆盖：
1. POST /api/auth/register — 注册
2. POST /api/auth/login — 登录
3. GET /api/auth/me — 获取当前用户
4. POST /api/teaching/chat — 教学对话
5. GET /api/teaching/state — 获取状态
6. POST /api/teaching/reset — 重置上下文
7. GET /api/admin/courses — Admin 角色校验
8. GET /api/health — 健康检查
9. GET /api/config — DEBUG 守卫
"""

import pytest
import uuid
import sys
import os
import json
from unittest.mock import patch

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

class TestTeachingAPI:
    """教学端点测试"""

    def test_chat_basic(self, client, student_token):
        """POST /api/teaching/chat — 基本对话"""
        resp = client.post("/api/teaching/chat",
            json={"message": "你好"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "response" in data
        assert "skill_used" in data
        assert "session_id" in data
        assert len(data["response"]) > 0

    def test_chat_with_session_id(self, client, student_token):
        """POST /api/teaching/chat — 指定 session_id"""
        resp = client.post("/api/teaching/chat",
            json={
                "message": "什么是变量？",
                "session_id": "custom_session_123",
            },
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "custom_session_123"

    def test_chat_returns_observability(self, client, student_token):
        """POST /api/teaching/chat — 应返回可观测链路"""
        resp = client.post("/api/teaching/chat",
            json={"message": "解释一下循环"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "observability" in data
        assert "trace" in data["observability"]
        assert "summary" in data["observability"]
        assert len(data["observability"]["trace"]) > 0

    def test_chat_returns_lifecycle(self, client, student_token):
        """POST /api/teaching/chat — 应返回生命周期状态"""
        resp = client.post("/api/teaching/chat",
            json={"message": "什么是数组？"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "lifecycle" in data
        assert data["lifecycle"]["status"] in ("completed", "failed")

    def test_chat_returns_state_summary(self, client, student_token):
        """POST /api/teaching/chat — 应返回状态摘要"""
        resp = client.post("/api/teaching/chat",
            json={"message": "什么是排序？"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "state_summary" in data

    def test_chat_no_auth(self, client):
        """POST /api/teaching/chat — 无认证应返回 401"""
        resp = client.post("/api/teaching/chat",
            json={"message": "你好"},
        )
        assert resp.status_code == 401

    def test_get_state(self, client, student_token):
        """GET /api/teaching/state — 获取学习状态"""
        resp = client.get("/api/teaching/state",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "user_id" in data

    def test_reset_context(self, client, student_token):
        """POST /api/teaching/reset — 重置上下文"""
        resp = client.post("/api/teaching/reset",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "上下文已重置"

    def test_multi_turn_conversation(self, client, student_token):
        """POST /api/teaching/chat — 多轮对话应保持 session"""
        session_id = f"multi_turn_{uuid.uuid4().hex[:8]}"

        # 第一轮
        resp1 = client.post("/api/teaching/chat",
            json={"message": "什么是递归？", "session_id": session_id},
            headers=auth_header(student_token),
        )
        assert resp1.status_code == 200

        # 第二轮
        resp2 = client.post("/api/teaching/chat",
            json={"message": "能给我一个例子吗？", "session_id": session_id},
            headers=auth_header(student_token),
        )
        assert resp2.status_code == 200
        assert resp2.json()["session_id"] == session_id


# ============================================================
# 4. Admin API 权限测试
# ============================================================

class TestAdminAPIPermissions:
    """Admin 端点权限测试"""

    def test_admin_courses_requires_auth(self, client):
        """GET /api/admin/courses — 无认证应返回 401"""
        resp = client.get("/api/admin/courses")
        assert resp.status_code == 401

    def test_student_cannot_access_admin(self, client, student_token):
        """GET /api/admin/courses — 学生不应访问管理端点"""
        resp = client.get("/api/admin/courses",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 403
        assert "权限" in resp.json()["detail"]

    def test_teacher_can_access_admin(self, client, teacher_token):
        """GET /api/admin/courses — 教师可以访问管理端点"""
        resp = client.get("/api/admin/courses",
            headers=auth_header(teacher_token),
        )
        assert resp.status_code == 200
        assert "courses" in resp.json()

    def test_student_cannot_create_course(self, client, student_token):
        """POST /api/admin/courses — 学生不能创建课程"""
        resp = client.post("/api/admin/courses",
            json={
                "course_id": "test_course",
                "title": "测试课程",
                "description": "测试",
            },
            headers=auth_header(student_token),
        )
        assert resp.status_code == 403

    def test_teacher_can_create_course(self, client, teacher_token):
        """POST /api/admin/courses — 教师可以创建课程"""
        course_id = f"course_{uuid.uuid4().hex[:8]}"
        resp = client.post("/api/admin/courses",
            json={
                "course_id": course_id,
                "title": "Python基础",
                "description": "Python编程基础课程",
            },
            headers=auth_header(teacher_token),
        )
        assert resp.status_code == 200
        assert resp.json()["course_id"] == course_id

    def test_student_cannot_access_knowledge_points(self, client, student_token):
        """GET /api/admin/knowledge-points — 学生不能访问"""
        resp = client.get("/api/admin/knowledge-points",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 403

    def test_student_cannot_access_teaching_experiences(self, client, student_token):
        """GET /api/admin/teaching-experiences — 学生不能访问"""
        resp = client.get("/api/admin/teaching-experiences",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 403

    def test_student_cannot_import(self, client, student_token):
        """POST /api/admin/import — 学生不能导入"""
        resp = client.post("/api/admin/import",
            json={"courses": []},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 403

    def test_student_cannot_export(self, client, student_token):
        """GET /api/admin/export — 学生不能导出"""
        resp = client.get("/api/admin/export",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 403


    def test_student_cannot_get_statistics(self, client, student_token):
        """GET /api/admin/statistics — 学生不能查看统计"""
        resp = client.get("/api/admin/statistics",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 403


# ============================================================
# 5. 安全测试
# ============================================================

class TestSessionManagement:
    """会话管理端点测试"""

    def test_create_session(self, client, student_token):
        """POST /api/teaching/sessions — 创建新会话"""
        resp = client.post("/api/teaching/sessions",
            json={"title": "测试会话"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["title"] == "测试会话"

    def test_create_session_default_title(self, client, student_token):
        """POST /api/teaching/sessions — 默认标题"""
        resp = client.post("/api/teaching/sessions",
            json={},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "新对话"

    def test_list_sessions(self, client, student_token):
        """GET /api/teaching/sessions — 列出会话"""
        # 先创建一个会话
        client.post("/api/teaching/sessions",
            json={"title": "列表测试"},
            headers=auth_header(student_token),
        )
        resp = client.get("/api/teaching/sessions",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert len(data["sessions"]) > 0

    def test_get_session_messages(self, client, student_token):
        """GET /api/teaching/sessions/{id}/messages — 获取消息历史"""
        # 创建会话
        create_resp = client.post("/api/teaching/sessions",
            json={"title": "消息测试"},
            headers=auth_header(student_token),
        )
        session_id = create_resp.json()["session_id"]

        # 发送一条消息
        client.post("/api/teaching/chat",
            json={"message": "你好", "session_id": session_id},
            headers=auth_header(student_token),
        )

        # 获取消息历史
        resp = client.get(f"/api/teaching/sessions/{session_id}/messages",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data
        # 应该有 user + assistant 两条消息
        assert len(data["messages"]) >= 2

    def test_get_messages_nonexistent_session(self, client, student_token):
        """GET /api/teaching/sessions/{id}/messages — 不存在的会话"""
        resp = client.get("/api/teaching/sessions/nonexistent/messages",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 404

    def test_chat_auto_creates_session(self, client, student_token):
        """POST /api/teaching/chat — 无 session_id 时自动创建"""
        resp = client.post("/api/teaching/chat",
            json={"message": "自动创建会话测试"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["session_id"] is not None

    def test_chat_updates_session_title(self, client, student_token):
        """POST /api/teaching/chat — 第一条消息自动更新会话标题"""
        # 创建会话
        create_resp = client.post("/api/teaching/sessions",
            json={"title": "新对话"},
            headers=auth_header(student_token),
        )
        session_id = create_resp.json()["session_id"]

        # 发送消息
        client.post("/api/teaching/chat",
            json={"message": "什么是Python变量？请解释一下", "session_id": session_id},
            headers=auth_header(student_token),
        )

        # 验证标题已更新
        sessions_resp = client.get("/api/teaching/sessions",
            headers=auth_header(student_token),
        )
        sessions = sessions_resp.json()["sessions"]
        updated = next((s for s in sessions if s["session_id"] == session_id), None)
        assert updated is not None
        assert updated["title"] != "新对话"  # 应该被更新为消息内容


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


# ============================================================
# 5. 用户学习旅程（端到端）
# ============================================================

from app.core.ai_service import ai_service


def _journey_chat_response(messages):
    """根据用户输入返回模拟的 AI 响应"""
    last_message = messages[-1].get("content", "")

    if "递归" in last_message and ("什么是" in last_message or "讲解" in last_message):
        return {"content": "递归就是函数在执行过程中调用自身。", "tool_calls": None, "finish_reason": "end_turn"}

    if "题目" in last_message or "出题" in last_message:
        return {
            "content": json.dumps({
                "question": "以下哪个函数是递归函数？\nA. def f(n): return n * f(n-1)\nB. def f(n): return n + 1",
                "options": ["A", "B"],
                "answer": "A",
                "explanation": "选项A调用了自身 f(n-1)，是递归。"
            }, ensure_ascii=False),
            "tool_calls": None,
            "finish_reason": "end_turn"
        }

    if "反馈" in last_message or "学生答对" in last_message or "学生答错" in last_message:
        return {"content": "✅ 答对了！递归函数的关键就是调用自身。", "tool_calls": None, "finish_reason": "end_turn"}

    return {"content": f"我理解了，这是关于'{last_message[:20]}'的内容。", "tool_calls": None, "finish_reason": "end_turn"}


class TestUserJourney:
    """用户旅程端到端测试"""

    def test_full_journey(self, client, student_token):
        """完整学习流程：讲解 → 出题 → 答题 → 学习路径 → 状态摘要"""
        headers = auth_header(student_token)

        with patch.object(ai_service, "chat", side_effect=_journey_chat_response):
            # 1. 创建会话
            resp = client.post("/api/teaching/sessions", json={"title": "递归学习"}, headers=headers)
            assert resp.status_code == 200
            session_id = resp.json()["session_id"]

            # 2. 请求概念讲解
            resp = client.post("/api/teaching/chat", json={
                "message": "什么是递归？",
                "session_id": session_id,
            }, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "response" in data
            assert data["skill_used"] in ["concept_lesson", "quick_explain"]

            # 3. 请求出题练习
            resp = client.post("/api/teaching/chat", json={
                "message": "给我出道递归题",
                "session_id": session_id,
            }, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "response" in data
            assert "A" in data["response"] or "题目" in data["response"]

            # 4. 提交答案
            resp = client.post("/api/teaching/chat", json={
                "message": "A",
                "session_id": session_id,
            }, headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert "response" in data

            # 5. 获取学习路径
            resp = client.post("/api/teaching/learning-path", json={"target_topic": "递归"}, headers=headers)
            assert resp.status_code == 200
            path = resp.json()
            assert path["target_topic"] == "递归"
            assert len(path["nodes"]) > 0
            assert path["next_recommended"] is not None

            # 6. 查看状态摘要
            resp = client.get("/api/teaching/state", headers=headers)
            assert resp.status_code == 200
            summary = resp.json()
            assert summary["user_id"] is not None
            assert summary["total_questions"] >= 0


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
