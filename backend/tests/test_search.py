"""
Search API 测试

覆盖主题资料搜索、自动导入、指定 URL 导入。
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
    username = f"search_student_{uuid.uuid4().hex[:8]}"
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
            "title": "Rust 异步编程",
            "description": "学习 Rust 的 async/await 与并发模型",
            "level": "intermediate",
            "daily_minutes": 60,
        },
        headers=auth_header(student_token),
    )
    assert resp.status_code == 201
    return resp.json()


def auth_header(token: str) -> dict:
    """生成认证头"""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mock_search(monkeypatch):
    """模拟 Tavily 搜索结果"""
    async def fake_search(*args, **kwargs):
        return [
            {
                "title": "Rust 异步编程指南",
                "url": "https://example.com/rust-async",
                "content": "这是一份 Rust 异步编程指南。",
                "score": 0.95,
            },
            {
                "title": "Tokio 官方文档",
                "url": "https://example.com/tokio",
                "content": "Tokio 是 Rust 的异步运行时。",
                "score": 0.88,
            },
        ]

    monkeypatch.setattr("app.api.search.WebSearchService.search", fake_search)


@pytest.fixture
def mock_fetch_url(monkeypatch):
    """模拟 Jina Reader 网页提取"""
    async def fake_fetch_url(self, url: str) -> str:
        return f"这是从 {url} 提取的网页正文内容。"

    monkeypatch.setattr("app.api.search.WebSearchService.fetch_url", fake_fetch_url)


@pytest.fixture
def mock_process_document(monkeypatch):
    """模拟文档向量化处理，避免测试时真正调用 embedding 模型"""
    async def fake_process(*args, **kwargs):
        return {"chunk_count": 3, "char_count": 100}

    monkeypatch.setattr("app.api.search.process_document", fake_process)


class TestTopicSearch:
    """主题搜索测试"""

    def test_search_without_auto_import(
        self, client, student_token, topic, mock_search
    ):
        """POST /api/topics/{id}/search — 仅搜索不导入"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/search",
            json={"max_results": 5, "auto_import": False},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["topic_id"] == topic_id
        assert "Rust" in data["query"]
        assert data["total_results"] == 2
        assert len(data["results"]) == 2
        assert data["imported_count"] == 0
        assert data["results"][0]["url"]

    def test_search_with_auto_import(
        self,
        client,
        student_token,
        topic,
        mock_search,
        mock_fetch_url,
        mock_process_document,
    ):
        """POST /api/topics/{id}/search — 搜索并自动导入"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/search",
            json={"max_results": 5, "auto_import": True, "import_limit": 2},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_count"] == 2

        # 验证资料已创建
        list_resp = client.get(
            f"/api/topics/{topic_id}/documents",
            headers=auth_header(student_token),
        )
        assert list_resp.status_code == 200
        docs = list_resp.json()["documents"]
        assert len(docs) == 2
        assert docs[0]["doc_type"] == "url"
        assert docs[0]["source_url"].startswith("https://")

    def test_search_other_user_topic(self, client, topic, mock_search):
        """不能搜索别人的主题"""
        other_username = f"other_search_{uuid.uuid4().hex[:8]}"
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
            f"/api/topics/{topic_id}/search",
            json={"auto_import": False},
            headers=auth_header(other_token),
        )
        assert resp.status_code == 403


class TestImportUrl:
    """URL 导入测试"""

    def test_import_url_success(
        self,
        client,
        student_token,
        topic,
        mock_fetch_url,
        mock_process_document,
    ):
        """POST /api/topics/{id}/import-url — 导入指定网页"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/import-url",
            json={
                "url": "https://example.com/rust-book",
                "title": "Rust 官方教程",
            },
            headers=auth_header(student_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["topic_id"] == topic_id
        assert data["url"] == "https://example.com/rust-book"
        assert data["title"] == "Rust 官方教程"
        assert data["status"] == "pending"
        assert "document_id" in data
