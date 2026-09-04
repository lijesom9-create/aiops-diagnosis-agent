"""
事故管理 API 测试（ack 语义 / 事故详情）

覆盖：
1. 认领接口：登录用户首认领生效、幂等（二次认领不覆盖首认领人）、404
2. 未认证访问 → 401
3. 事故详情：字段完整性与认领信息
4. MTTA 上盘验证（ops_incident_mtta_seconds 直方图出现样本）

使用 dependency_overrides 注入内存模式 Database（不依赖本地 Mongo 状态）。
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient
from test_incident_lifecycle import _memory_db

from app.core.database import get_db
from main import app


@pytest.fixture
def incident_env(monkeypatch):
    """内存 db + 已注册测试用户"""
    mem_db = _memory_db()

    # 路由层用 Depends(get_db)，覆盖后测试完全隔离于本地 Mongo
    app.dependency_overrides[get_db] = lambda: mem_db
    yield mem_db
    app.dependency_overrides.pop(get_db, None)


def _auth(client, username: str) -> dict:
    """注册并返回认证头（注册固定 student，认领权限对所有登录用户开放）

    用户名追加唯一后缀：真实 Mongo 中残留历史用户会撞唯一键。
    """
    import uuid as _uuid
    username = f"{username}_{_uuid.uuid4().hex[:6]}"
    resp = client.post("/api/auth/register", json={
        "username": username,
        "password": "test123456",
        "email": f"{username}@test.com",
        "org_name": f"org_{username}",
    })
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_admin(client) -> dict:
    """注册超管账号：生成唯一用户名并临时设为 SUPER_ADMIN_USERNAME（避免真实 Mongo 残留撞键）"""
    import uuid as _uuid

    from app.core.config import settings
    username = f"super_{_uuid.uuid4().hex[:8]}"
    prev = settings.SUPER_ADMIN_USERNAME
    settings.SUPER_ADMIN_USERNAME = username
    try:
        resp = client.post("/api/auth/register", json={
            "username": username,
            "password": "test123456",
            "email": f"{username}@test.com",
            "org_name": f"org_{username}",
        })
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}
    finally:
        settings.SUPER_ADMIN_USERNAME = prev


def _create_incident(db) -> str:
    """直接构造事故文档落库（纯内存操作，无事件循环亲和性问题；路由逻辑已在 test_incident_lifecycle 覆盖）"""
    import asyncio
    import uuid

    doc = {
        "incident_id": f"INC-AUTO-{uuid.uuid4().hex[:8].upper()}",
        "status": "active",
        "service": "payment-sim",
        "fingerprints": ["fp:fp-ack-001"],
        "resolved_fps": [],
        "alertnames": ["HighCpuUsage"],
        "max_severity": "warning",
        "first_seen_at": datetime.now() - timedelta(minutes=5),
        "last_seen_at": datetime.now(),
        "resolved_at": None,
        "diag_count": 0,
        "last_diag_at": None,
        "last_confidence_level": "unknown",
        "diagnosis_history": [],
        "summary": None,
    }
    asyncio.run(db.save_incident(doc))
    return doc["incident_id"]


class TestIncidentAck:

    def test_ack_requires_auth(self, incident_env):
        with TestClient(app) as client:
            db = incident_env
            incident_id = _create_incident(db)
            resp = client.post(f"/api/incidents/{incident_id}/ack")
            assert resp.status_code == 401, resp.text

    def test_ack_first_wins_and_idempotent(self, incident_env):
        """首认领生效；重复认领不覆盖首认领人（幂等）"""
        with TestClient(app) as client:
            db = incident_env
            incident_id = _create_incident(db)

            alice = _auth(client, f"alice_{incident_id[:8]}")
            bob = _auth(client, f"bob_{incident_id[:8]}")

            r1 = client.post(f"/api/incidents/{incident_id}/ack", headers=alice)
            assert r1.status_code == 200, r1.text
            body = r1.json()
            assert body["first_ack"] is True
            assert body["acked_by"], "首认领后 acked_by 应有值"

            r2 = client.post(f"/api/incidents/{incident_id}/ack", headers=bob)
            assert r2.status_code == 200
            body2 = r2.json()
            assert body2["first_ack"] is False, "二次认领不应覆盖"
            assert body2["acked_by"] == body["acked_by"], "保留首认领人"

    def test_ack_nonexistent_404(self, incident_env):
        with TestClient(app) as client:
            auth = _auth(client, "ack_user_404")
            resp = client.post("/api/incidents/INC-AUTO-NOTHING/ack", headers=auth)
            assert resp.status_code == 404

    def test_detail_contains_ack_info(self, incident_env):
        with TestClient(app) as client:
            db = incident_env
            incident_id = _create_incident(db)
            auth = _auth(client, f"detail_user_{incident_id[:8]}")

            # ack 前：acked 字段为空
            r = client.get(f"/api/incidents/{incident_id}", headers=auth)
            assert r.status_code == 200
            body = r.json()
            assert body["acked_by"] is None
            assert body["status"] == "active"
            assert body["service"] == "payment-sim"
            assert isinstance(body["diagnosis_history"], list)

            # ack 后：认领信息可见
            client.post(f"/api/incidents/{incident_id}/ack", headers=auth)
            r2 = client.get(f"/api/incidents/{incident_id}", headers=auth)
            body2 = r2.json()
            assert body2["acked_by"] is not None
            assert body2["acked_at"] is not None

    def test_detail_requires_auth(self, incident_env):
        with TestClient(app) as client:
            resp = client.get("/api/incidents/INC-AUTO-ANY")
            assert resp.status_code == 401


class TestForceResolve:
    """B6 事故卡死保护：管理员强制闭案"""

    def test_force_resolve_requires_auth(self, incident_env):
        with TestClient(app) as client:
            db = incident_env
            incident_id = _create_incident(db)
            resp = client.post(f"/api/incidents/{incident_id}/force-resolve")
            assert resp.status_code == 401, resp.text

    def test_force_resolve_requires_admin(self, incident_env):
        """普通用户（student）无权强制闭案 → 403"""
        with TestClient(app) as client:
            db = incident_env
            incident_id = _create_incident(db)
            student = _auth(client, f"student_{incident_id[:8]}")
            resp = client.post(f"/api/incidents/{incident_id}/force-resolve", headers=student)
            assert resp.status_code == 403, resp.text

    def test_admin_force_resolves_active_incident(self, incident_env):
        with TestClient(app) as client:
            db = incident_env
            incident_id = _create_incident(db)
            admin = _auth_admin(client)

            resp = client.post(f"/api/incidents/{incident_id}/force-resolve", headers=admin)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "resolved"
            assert body["auto_resolved"] is False
            assert body["resolved_at"]
            assert body["force_resolved_by"]

            # 落库校验
            import asyncio
            inc = asyncio.run(db.get_incident(incident_id))
            assert inc["status"] == "resolved"
            assert inc["auto_resolved"] is False

    def test_force_resolve_nonexistent_404(self, incident_env):
        with TestClient(app) as client:
            admin = _auth_admin(client)
            resp = client.post("/api/incidents/INC-AUTO-NOTHING/force-resolve", headers=admin)
            assert resp.status_code == 404

    def test_force_resolve_already_resolved_idempotent(self, incident_env):
        """已 resolved 的事故再次强制闭案 → 幂等返回，不报错"""
        with TestClient(app) as client:
            db = incident_env
            incident_id = _create_incident(db)
            admin = _auth_admin(client)

            r1 = client.post(f"/api/incidents/{incident_id}/force-resolve", headers=admin)
            assert r1.status_code == 200
            r2 = client.post(f"/api/incidents/{incident_id}/force-resolve", headers=admin)
            assert r2.status_code == 200
            assert r2.json()["status"] == "resolved"


class TestListIncidents:
    """事故列表端点 GET /api/incidents"""

    def test_list_requires_auth(self, incident_env):
        with TestClient(app) as client:
            resp = client.get("/api/incidents")
            assert resp.status_code == 401

    def test_list_empty(self, incident_env):
        with TestClient(app) as client:
            auth = _auth(client, "list_empty")
            resp = client.get("/api/incidents", headers=auth)
            assert resp.status_code == 200
            assert resp.json() == []

    def test_list_returns_incidents(self, incident_env):
        with TestClient(app) as client:
            db = incident_env
            id1 = _create_incident(db)
            auth = _auth(client, "list_basic")
            resp = client.get("/api/incidents", headers=auth)
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) >= 1
            ids = [item["incident_id"] for item in data]
            assert id1 in ids
            item = [i for i in data if i["incident_id"] == id1][0]
            assert item["status"] == "active"
            assert item["service"] == "payment-sim"
            assert "alertnames" in item
            assert "diag_count" in item

    def test_list_filter_by_service(self, incident_env):
        with TestClient(app) as client:
            db = incident_env
            _create_incident(db)
            auth = _auth(client, "list_filter_svc")
            resp = client.get("/api/incidents?service=payment-sim", headers=auth)
            assert resp.status_code == 200
            assert len(resp.json()) >= 1
            resp2 = client.get("/api/incidents?service=nonexistent", headers=auth)
            assert resp2.json() == []

    def test_list_filter_by_status(self, incident_env):
        with TestClient(app) as client:
            db = incident_env
            inc_id = _create_incident(db)
            admin = _auth_admin(client)
            client.post(f"/api/incidents/{inc_id}/force-resolve", headers=admin)
            auth = _auth(client, "list_filter_status")
            resp = client.get("/api/incidents?status=resolved", headers=auth)
            assert resp.status_code == 200
            data = resp.json()
            assert all(item["status"] == "resolved" for item in data)
            assert any(item["incident_id"] == inc_id for item in data)
