"""
Study Plan API 测试

覆盖学习计划生成、查询、更新、归档、删除。
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
    """创建测试客户端"""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def student_token(client):
    """注册一个学生用户并返回 token"""
    username = f"plan_student_{uuid.uuid4().hex[:8]}"
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
def topic(client, student_token):
    """创建一个测试主题"""
    resp = client.post(
        "/api/topics",
        json={
            "title": "Python 数据分析",
            "description": "学习 Pandas、NumPy 和 Matplotlib",
            "level": "beginner",
            "daily_minutes": 45,
            "target_date": "2026-12-31",
        },
        headers=auth_header(student_token),
    )
    assert resp.status_code == 201
    return resp.json()


def auth_header(token: str) -> dict:
    """生成认证头"""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mock_ai_plan(monkeypatch):
    """模拟 AI 生成的学习计划 JSON"""
    async def fake_chat(*args, **kwargs):
        return {
            "content": """{
                "title": "Python 数据分析学习计划",
                "description": "四周入门 Python 数据分析",
                "phases": [
                    {
                        "title": "第一周：Python 基础",
                        "description": "复习 Python 基础语法",
                        "duration_days": 7,
                        "tasks": [
                            {"title": "变量与数据类型", "description": "学习基础语法", "estimated_minutes": 30, "type": "read"},
                            {"title": "条件与循环", "description": "练习控制流", "estimated_minutes": 30, "type": "practice"}
                        ]
                    },
                    {
                        "title": "第二周：NumPy",
                        "description": "掌握 NumPy 数组操作",
                        "duration_days": 7,
                        "tasks": [
                            {"title": "数组创建", "description": "学习 ndarray", "estimated_minutes": 30, "type": "read"}
                        ]
                    }
                ]
            }"""
        }

    monkeypatch.setattr("app.services.study_plan.ai_service.chat", fake_chat)


class TestStudyPlanGenerate:
    """学习计划生成测试"""

    def test_generate_plan_success(self, client, student_token, topic, mock_ai_plan):
        """POST /api/topics/{id}/plans/generate — 生成学习计划"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/plans/generate",
            json={},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["topic_id"] == topic_id
        assert data["title"]
        assert len(data["phases"]) > 0
        assert data["total_tasks"] >= 2
        assert data["daily_minutes"] == 45

    def test_generate_plan_other_user_topic(self, client, topic, mock_ai_plan):
        """不能为别人的主题生成计划"""
        other_username = f"other_plan_{uuid.uuid4().hex[:8]}"
        resp = client.post("/api/auth/register", json={
            "username": other_username,
            "password": "test123456",
            "email": f"{other_username}@test.com",
            "role": "student",
        "org_name": f"org_{uuid.uuid4().hex[:8]}",
        })
        other_token = resp.json()["access_token"]

        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/plans/generate",
            json={},
            headers=auth_header(other_token),
        )
        assert resp.status_code == 403


class TestStudyPlanCRUD:
    """学习计划 CRUD 测试"""

    @pytest.fixture(scope="function")
    def created_plan(self, client, student_token, topic, mock_ai_plan):
        """生成一个学习计划"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/plans/generate",
            json={},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 201
        return resp.json()

    def test_list_topic_plans(self, client, student_token, topic, created_plan):
        """GET /api/topics/{id}/plans — 获取主题计划列表"""
        topic_id = topic["topic_id"]
        resp = client.get(
            f"/api/topics/{topic_id}/plans",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["plans"][0]["plan_id"] == created_plan["plan_id"]

    def test_get_plan(self, client, student_token, created_plan):
        """GET /api/plans/{id} — 获取计划详情"""
        plan_id = created_plan["plan_id"]
        resp = client.get(
            f"/api/plans/{plan_id}",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["plan_id"] == plan_id

    def test_update_plan(self, client, student_token, created_plan):
        """PUT /api/plans/{id} — 更新计划"""
        plan_id = created_plan["plan_id"]
        resp = client.put(
            f"/api/plans/{plan_id}",
            json={"title": "更新后的标题"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "更新后的标题"

    def test_archive_and_activate_plan(self, client, student_token, created_plan):
        """归档/激活学习计划"""
        plan_id = created_plan["plan_id"]

        resp = client.post(
            f"/api/plans/{plan_id}/archive",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

        resp = client.post(
            f"/api/plans/{plan_id}/activate",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_delete_plan(self, client, student_token, created_plan):
        """DELETE /api/plans/{id} — 删除计划"""
        plan_id = created_plan["plan_id"]
        resp = client.delete(
            f"/api/plans/{plan_id}",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 204

        resp = client.get(
            f"/api/plans/{plan_id}",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 404
