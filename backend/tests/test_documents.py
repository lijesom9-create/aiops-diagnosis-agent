"""
Document API 测试

覆盖资料上传、状态查询、列表、删除。
"""

import pytest
import uuid
import sys
import os
import time

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
    username = f"doc_student_{uuid.uuid4().hex[:8]}"
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
        json={"title": "文档测试主题"},
        headers=auth_header(student_token),
    )
    assert resp.status_code == 201
    return resp.json()


def auth_header(token: str) -> dict:
    """生成认证头"""
    return {"Authorization": f"Bearer {token}"}


def make_txt_file(content: str = "Python 是一种广泛使用的编程语言。\n它支持多种编程范式。\n"):
    """构造一个上传用的 TXT 文件"""
    from io import BytesIO
    return BytesIO(content.encode("utf-8"))


class TestDocumentUpload:
    """文档上传测试"""

    def test_upload_txt_document(self, client, student_token, topic):
        """POST /api/topics/{id}/documents — 上传 TXT 文档"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/documents",
            files={"file": ("test.txt", make_txt_file(), "text/plain")},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["filename"] == "test.txt"
        assert data["topic_id"] == topic_id
        assert data["status"] in ("pending", "processing", "completed")
        assert "document_id" in data

    def test_upload_no_auth(self, client, topic):
        """POST /api/topics/{id}/documents — 无认证应失败"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/documents",
            files={"file": ("test.txt", make_txt_file(), "text/plain")},
        )
        assert resp.status_code == 401

    def test_upload_to_other_user_topic(self, client, topic):
        """POST /api/topics/{id}/documents — 不能上传到别人的主题"""
        # 创建另一个用户
        other_username = f"other_doc_{uuid.uuid4().hex[:8]}"
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
            f"/api/topics/{topic_id}/documents",
            files={"file": ("test.txt", make_txt_file(), "text/plain")},
            headers=auth_header(other_token),
        )
        assert resp.status_code == 403

    def test_upload_empty_file(self, client, student_token, topic):
        """POST /api/topics/{id}/documents — 空文件应失败"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/documents",
            files={"file": ("empty.txt", b"", "text/plain")},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 400

    def test_upload_unsupported_format(self, client, student_token, topic):
        """POST /api/topics/{id}/documents — 不支持的格式最终应标记为 failed"""
        topic_id = topic["topic_id"]
        resp = client.post(
            f"/api/topics/{topic_id}/documents",
            files={"file": ("test.xyz", b"some content", "application/octet-stream")},
            headers=auth_header(student_token),
        )
        assert resp.status_code == 201
        document_id = resp.json()["document_id"]

        # 轮询状态，直到处理完成或失败
        for _ in range(10):
            status_resp = client.get(
                f"/api/documents/{document_id}/status",
                headers=auth_header(student_token),
            )
            if status_resp.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)

        assert status_resp.json()["status"] == "failed"


class TestDocumentStatusAndList:
    """文档状态与列表测试"""

    def test_get_document_status(self, client, student_token, topic):
        """GET /api/documents/{id}/status — 获取状态"""
        topic_id = topic["topic_id"]
        upload_resp = client.post(
            f"/api/topics/{topic_id}/documents",
            files={"file": ("test.txt", make_txt_file(), "text/plain")},
            headers=auth_header(student_token),
        )
        document_id = upload_resp.json()["document_id"]

        resp = client.get(
            f"/api/documents/{document_id}/status",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["document_id"] == document_id
        assert "status" in data

    def test_list_topic_documents(self, client, student_token, topic):
        """GET /api/topics/{id}/documents — 列出主题文档"""
        topic_id = topic["topic_id"]

        # 上传一个文档
        client.post(
            f"/api/topics/{topic_id}/documents",
            files={"file": ("test.txt", make_txt_file(), "text/plain")},
            headers=auth_header(student_token),
        )

        resp = client.get(
            f"/api/topics/{topic_id}/documents",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "documents" in data
        assert data["total"] >= 1
        assert all(d["topic_id"] == topic_id for d in data["documents"])


class TestDocumentDelete:
    """文档删除测试"""

    def test_delete_document(self, client, student_token, topic):
        """DELETE /api/topics/{id}/documents/{doc_id} — 删除文档"""
        topic_id = topic["topic_id"]
        upload_resp = client.post(
            f"/api/topics/{topic_id}/documents",
            files={"file": ("test.txt", make_txt_file(), "text/plain")},
            headers=auth_header(student_token),
        )
        document_id = upload_resp.json()["document_id"]

        resp = client.delete(
            f"/api/topics/{topic_id}/documents/{document_id}",
            headers=auth_header(student_token),
        )
        assert resp.status_code == 204

        # 删除后应无法访问
        status_resp = client.get(
            f"/api/documents/{document_id}/status",
            headers=auth_header(student_token),
        )
        assert status_resp.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
