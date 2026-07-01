"""
Topic API 测试

覆盖学习主题的 CRUD、权限和状态管理。
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
    username = f"topic_student_{uuid.uuid4().hex[:8]}"
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
def other_student_token(client):
    """注册另一个学生用户并返回 token"""
    username = f"other_student_{uuid.uuid4().hex[:8]}"
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
    """生成认证头"""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="function")
def created_topic(client, student_token):
    """创建一个测试主题并返回其数据"""
    resp = client.post(
        "/api/topics",
        json={
            "title": "Python 入门",
            "description": "学习 Python 基础语法",
            "level": "beginner",
            "daily_minutes": 45,
            "target_date": "2026-12-31",
        },
        headers=auth_header(student_token),
    )
    assert resp.status_code == 201
    return resp.json()


class TestTopicCRUD:
    """主题 CRUD 测试"""

    def test_create_topic(self, client, student_token):
        """POST /api/topics — 创建主题"""
        resp = client.post(
            "/api/topics",
            json={
                "title": "机器学习",
                "description": "从零开始学 ML",
                "level": "beginner",
                "daily_minutes": 60,
            },
            headers=auth_header(student_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "机器学习"
        assert data["description"] == "从零开始学 ML"
        assert data["level"] == "beginner"
        assert data["daily_minutes"] == 60
        assert data["status"] == "active"
        assert "topic_id" in data

    def test_create_topic_minimal(self, client, student_token):
        """POST /api/topics — 只填标题也能创建"""
        resp = client.post(
            "/api/topics",
            json={"title": "极简主题"},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "极简主题"
        assert data["daily_minutes"] == 30  # 默认值
        assert data["level"] == "beginner"  # 默认值

    def test_create_topic_no_auth(self, client):
        """POST /api/topics — 无认证应失败"""
        resp = client.post("/api/topics", json={"title": "无认证"})
        assert resp.status_code == 401

    def test_create_topic_empty_title(self, client, student_token):
        """POST /api/topics — 空标题应失败"""
        resp = client.post(
            "/api/topics",
            json={"title": ""},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 422

    def test_list_topics(self, client, student_token, created_topic):
        """GET /api/topics — 列出主题"""
        resp = client.get("/api/topics", headers=auth_header(student_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "topics" in data
        assert data["total"] >= 1
        assert any(t["topic_id"] == created_topic["topic_id"] for t in data["topics"])

    def test_list_topics_filter_by_status(self, client, student_token, created_topic):
        """GET /api/topics?status=active — 按状态过滤"""
        resp = client.get(
            "/api/topics?status=active",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(t["status"] == "active" for t in data["topics"])

    def test_get_topic(self, client, student_token, created_topic):
        """GET /api/topics/{id} — 获取主题详情"""
        topic_id = created_topic["topic_id"]
        resp = client.get(f"/api/topics/{topic_id}", headers=auth_header(student_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["topic_id"] == topic_id
        assert data["title"] == "Python 入门"

    def test_get_topic_not_found(self, client, student_token):
        """GET /api/topics/{id} — 不存在的主题"""
        resp = client.get("/api/topics/nonexistent", headers=auth_header(student_token))
        assert resp.status_code == 404

    def test_get_topic_forbidden(self, client, other_student_token, created_topic):
        """GET /api/topics/{id} — 其他用户不能访问"""
        topic_id = created_topic["topic_id"]
        resp = client.get(f"/api/topics/{topic_id}", headers=auth_header(other_student_token))
        assert resp.status_code == 403

    def test_update_topic(self, client, student_token, created_topic):
        """PUT /api/topics/{id} — 更新主题"""
        topic_id = created_topic["topic_id"]
        resp = client.put(
            f"/api/topics/{topic_id}",
            json={
                "title": "Python 进阶",
                "daily_minutes": 90,
            },
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Python 进阶"
        assert data["daily_minutes"] == 90
        assert data["description"] == "学习 Python 基础语法"  # 未更新字段保持原值

    def test_update_topic_forbidden(self, client, other_student_token, created_topic):
        """PUT /api/topics/{id} — 其他用户不能更新"""
        topic_id = created_topic["topic_id"]
        resp = client.put(
            f"/api/topics/{topic_id}",
            json={"title": "恶意修改"},
            headers=auth_header(other_student_token),
        )
        assert resp.status_code == 403

    def test_delete_topic(self, client, student_token, created_topic):
        """DELETE /api/topics/{id} — 删除主题"""
        topic_id = created_topic["topic_id"]
        resp = client.delete(f"/api/topics/{topic_id}", headers=auth_header(student_token))
        assert resp.status_code == 204

        # 删除后应无法访问
        resp = client.get(f"/api/topics/{topic_id}", headers=auth_header(student_token))
        assert resp.status_code == 404

    def test_delete_topic_forbidden(self, client, other_student_token, created_topic):
        """DELETE /api/topics/{id} — 其他用户不能删除"""
        topic_id = created_topic["topic_id"]
        resp = client.delete(f"/api/topics/{topic_id}", headers=auth_header(other_student_token))
        assert resp.status_code == 403


class TestTopicStatus:
    """主题状态管理测试"""

    def test_archive_topic(self, client, student_token, created_topic):
        """POST /api/topics/{id}/archive — 归档主题"""
        topic_id = created_topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/archive",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

        # 归档后 active 过滤不应包含
        resp = client.get("/api/topics?status=active", headers=auth_header(student_token))
        assert topic_id not in [t["topic_id"] for t in resp.json()["topics"]]

    def test_activate_topic(self, client, student_token, created_topic):
        """POST /api/topics/{id}/activate — 激活主题"""
        topic_id = created_topic["topic_id"]

        # 先归档
        client.post(f"/api/topics/{topic_id}/archive", headers=auth_header(student_token))

        # 再激活
        resp = client.post(
            f"/api/topics/{topic_id}/activate",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
