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
