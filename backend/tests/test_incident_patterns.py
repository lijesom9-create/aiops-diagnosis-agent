"""
D1 根因模式统计测试

覆盖 GET /api/incidents/patterns：
1. 认证门禁（401）
2. 空库 → 空列表
3. 单事故单诊断 → count=1
4. 多事故同根因 → count=2，incident_ids 聚合
5. 同事故多次诊断同根因 → count=1（去重）
6. summary 条目排除（不参与统计）
7. 时间窗过滤：超出 days 的事故不统计
8. limit 参数限制返回条数
9. 按 count 降序排列
10. services 跨事故聚合

口径见 db.aggregate_incident_patterns。
"""

import os
import sys
import uuid as _uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient
from test_incident_lifecycle import _memory_db

from app.core.database import get_db
from main import app


@pytest.fixture
def incident_env(monkeypatch):
    """内存 db + 认证头"""
    mem_db = _memory_db()
    app.dependency_overrides[get_db] = lambda: mem_db
    yield mem_db
    app.dependency_overrides.pop(get_db, None)


def _auth(client, username: str) -> dict:
    """注册并返回认证头"""
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


def _make_incident(
    db, *, service="payment-sim", root_cause="慢查询", trigger="initial",
    incident_id=None, days_ago=0, extra_diag=None,
    sufficiency_level="medium", sufficiency_score=60,
    acked_by="tester", status="resolved",
):
    """构造事故文档并落库（含诊断历史）

    Args:
        extra_diag: 额外诊断条目列表（追加到 diagnosis_history）
        sufficiency_level/sufficiency_score: 主诊断条目的充分度
        acked_by: 认领人（None 表示未认领）
        status: 事故状态（active/resolved）
    """
    import asyncio

    now = datetime.now()
    first_seen = now - timedelta(days=days_ago)
    diag_entries = [{
        "trigger": trigger,
        "root_cause": root_cause,
        "at": first_seen + timedelta(minutes=5),
        "confidence_level": "medium",
        "sufficiency_score": sufficiency_score,
        "sufficiency_level": sufficiency_level,
    }]
    if extra_diag:
        diag_entries.extend(extra_diag)

    doc = {
        "incident_id": incident_id or f"INC-PAT-{_uuid.uuid4().hex[:8].upper()}",
        "status": status,
        "service": service,
        "fingerprints": [f"fp:{_uuid.uuid4().hex[:6]}"],
        "resolved_fps": [],
        "alertnames": ["HighLatency"],
        "max_severity": "warning",
        "first_seen_at": first_seen,
        "last_seen_at": now - timedelta(days=days_ago, minutes=-10),
        "resolved_at": now if status == "resolved" else None,
        "acked_by": acked_by,
        "acked_at": first_seen + timedelta(minutes=2) if acked_by else None,
        "diag_count": len(diag_entries),
        "last_diag_at": diag_entries[-1].get("at"),
        "last_confidence_level": "medium",
        "diagnosis_history": diag_entries,
        "summary": None,
    }
    asyncio.run(db.save_incident(doc))
    return doc["incident_id"]


class TestIncidentPatterns:

    def test_patterns_requires_auth(self, incident_env):
        """未认证 → 401"""
        with TestClient(app) as client:
            resp = client.get("/api/incidents/patterns")
            assert resp.status_code == 401

    def test_empty_db_returns_empty_list(self, incident_env):
        """空库 → 空列表（非 404，非 null）"""
        with TestClient(app) as client:
            auth = _auth(client, "pat_empty")
            resp = client.get("/api/incidents/patterns", headers=auth)
            assert resp.status_code == 200
            assert resp.json() == []

    def test_single_incident_single_diagnosis(self, incident_env):
        """单事故单诊断 → count=1"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_single")
            _make_incident(db, root_cause="数据库连接池耗尽")

            resp = client.get("/api/incidents/patterns", headers=auth)
            assert resp.status_code == 200
            patterns = resp.json()
            assert len(patterns) == 1
            assert patterns[0]["root_cause"] == "数据库连接池耗尽"
            assert patterns[0]["count"] == 1
            assert "payment-sim" in patterns[0]["services"]

    def test_multiple_incidents_same_root_cause(self, incident_env):
        """两事故同根因 → count=2，incident_ids 聚合两 ID"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_multi")
            id1 = _make_incident(db, root_cause="慢查询", service="svc-a")
            id2 = _make_incident(db, root_cause="慢查询", service="svc-b")

            resp = client.get("/api/incidents/patterns", headers=auth)
            patterns = resp.json()
            assert len(patterns) == 1
            assert patterns[0]["count"] == 2
            assert set(patterns[0]["incident_ids"]) == {id1, id2}
            assert set(patterns[0]["services"]) == {"svc-a", "svc-b"}

    def test_same_incident_multiple_diagnoses_deduped(self, incident_env):
        """同事故多次诊断同根因 → count=1（去重）

        初诊/重诊/再重诊若结论相同，按"一个事故一个根因"口径只计一次。
        """
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_dedup")
            _make_incident(
                db, root_cause="连接池耗尽",
                extra_diag=[
                    {"trigger": "escalation", "root_cause": "连接池耗尽",
                     "at": datetime.now(), "confidence_level": "high"},
                    {"trigger": "repeat", "root_cause": "连接池耗尽",
                     "at": datetime.now(), "confidence_level": "high"},
                ],
            )

            resp = client.get("/api/incidents/patterns", headers=auth)
            patterns = resp.json()
            assert len(patterns) == 1
            assert patterns[0]["count"] == 1, "同事故同根因多次诊断应去重为 1"

    def test_summary_entries_excluded(self, incident_env):
        """summary 条目不参与统计（避免与诊断条目重复计数）"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_summary")
            _make_incident(
                db, root_cause="初始根因",
                extra_diag=[
                    {"trigger": "summary", "root_cause": "摘要根因",
                     "at": datetime.now()},
                ],
            )

            resp = client.get("/api/incidents/patterns", headers=auth)
            patterns = resp.json()
            # 只应出现"初始根因"，不出现"摘要根因"
            assert len(patterns) == 1
            assert patterns[0]["root_cause"] == "初始根因"

    def test_date_window_filter(self, incident_env):
        """超出 days 的事故不统计"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_date")
            # 7 天前的事故
            _make_incident(db, root_cause="老根因", days_ago=7)
            # 今天的事故
            _make_incident(db, root_cause="新根因", days_ago=0)

            # days=1 → 只看今天
            resp = client.get(
                "/api/incidents/patterns?days=1", headers=auth,
            )
            patterns = resp.json()
            assert len(patterns) == 1
            assert patterns[0]["root_cause"] == "新根因"

    def test_limit_param(self, incident_env):
        """limit=N 限制返回条数（按 count 降序取前 N）"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_limit")
            _make_incident(db, root_cause="根因A")
            _make_incident(db, root_cause="根因B")
            _make_incident(db, root_cause="根因C")

            resp = client.get(
                "/api/incidents/patterns?limit=2", headers=auth,
            )
            patterns = resp.json()
            assert len(patterns) == 2

    def test_sorted_by_count_desc(self, incident_env):
        """按 count 降序排列——高频根因在前"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_sort")
            # 根因X 出现 1 次
            _make_incident(db, root_cause="根因X")
            # 根因Y 出现 3 次（三个不同事故）
            _make_incident(db, root_cause="根因Y", service="svc-y1")
            _make_incident(db, root_cause="根因Y", service="svc-y2")
            _make_incident(db, root_cause="根因Y", service="svc-y3")
            # 根因Z 出现 2 次
            _make_incident(db, root_cause="根因Z", service="svc-z1")
            _make_incident(db, root_cause="根因Z", service="svc-z2")

            resp = client.get("/api/incidents/patterns", headers=auth)
            patterns = resp.json()
            assert patterns[0]["root_cause"] == "根因Y"
            assert patterns[0]["count"] == 3
            assert patterns[1]["root_cause"] == "根因Z"
            assert patterns[1]["count"] == 2
            assert patterns[2]["root_cause"] == "根因X"
            assert patterns[2]["count"] == 1

    def test_first_and_last_seen_across_incidents(self, incident_env):
        """first_seen = 最早诊断时间，last_seen = 最晚诊断时间（跨事故）"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "pat_seen")
            # 第一个事故：2 天前
            _make_incident(db, root_cause="复现根因", days_ago=2)
            # 第二个事故：今天
            _make_incident(db, root_cause="复现根因", days_ago=0)

            resp = client.get("/api/incidents/patterns", headers=auth)
            patterns = resp.json()
            assert len(patterns) == 1
            assert patterns[0]["count"] == 2
            # first_seen 早于 last_seen
            assert patterns[0]["first_seen"]
            assert patterns[0]["last_seen"]
            assert patterns[0]["first_seen"] != patterns[0]["last_seen"]


class TestDiagnosisQuality:
    """D2 诊断质量分层统计——验证"高充分度 → 高采纳率"假设"""

    def test_quality_requires_auth(self, incident_env):
        """未认证 → 401"""
        with TestClient(app) as client:
            resp = client.get("/api/incidents/quality")
            assert resp.status_code == 401

    def test_empty_db_returns_empty_list(self, incident_env):
        """空库 → 空列表"""
        with TestClient(app) as client:
            auth = _auth(client, "qual_empty")
            resp = client.get("/api/incidents/quality", headers=auth)
            assert resp.status_code == 200
            assert resp.json() == []

    def test_single_incident_acked(self, incident_env):
        """单事故 high 充分度 + 已认领 → tier count=1, acked=1, ack_rate=1.0"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_single")
            _make_incident(
                db, root_cause="根因", sufficiency_level="high",
                sufficiency_score=85, acked_by="alice",
            )

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            assert len(tiers) == 1
            t = tiers[0]
            assert t["sufficiency_level"] == "high"
            assert t["count"] == 1
            assert t["acked"] == 1
            assert t["ack_rate"] == 1.0

    def test_ack_rate_calculation(self, incident_env):
        """ack_rate = acked / count：3 事故 2 认领 → 0.667"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_rate")
            _make_incident(db, root_cause="r", sufficiency_level="high", acked_by="a")
            _make_incident(db, root_cause="r", sufficiency_level="high", acked_by="b")
            _make_incident(db, root_cause="r", sufficiency_level="high", acked_by=None)

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            assert len(tiers) == 1
            t = tiers[0]
            assert t["count"] == 3
            assert t["acked"] == 2
            assert t["ack_rate"] == round(2 / 3, 3)

    def test_grouped_by_sufficiency_level(self, incident_env):
        """不同充分度分到不同层"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_group")
            _make_incident(db, root_cause="r", sufficiency_level="high", acked_by="a")
            _make_incident(db, root_cause="r", sufficiency_level="medium", acked_by="b")
            _make_incident(db, root_cause="r", sufficiency_level="low", acked_by=None)

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            levels = [t["sufficiency_level"] for t in tiers]
            assert "high" in levels
            assert "medium" in levels
            assert "low" in levels

    def test_sorted_high_to_low(self, incident_env):
        """按 high → medium → low → unknown 顺序排列"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_sort")
            _make_incident(db, root_cause="r", sufficiency_level="low")
            _make_incident(db, root_cause="r", sufficiency_level="high")
            _make_incident(db, root_cause="r", sufficiency_level="medium")

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            levels = [t["sufficiency_level"] for t in tiers]
            assert levels == ["high", "medium", "low"]

    def test_latest_non_summary_diagnosis_used(self, incident_env):
        """取最近一次非摘要诊断的 sufficiency_level（初诊 low → 重诊 high → 用 high）"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_latest")
            _make_incident(
                db, root_cause="r", sufficiency_level="low",
                extra_diag=[
                    {"trigger": "escalation", "root_cause": "r",
                     "at": datetime.now(), "confidence_level": "high",
                     "sufficiency_level": "high", "sufficiency_score": 80},
                ],
            )

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            assert len(tiers) == 1
            assert tiers[0]["sufficiency_level"] == "high", "应取最近一次诊断（重诊 high）"

    def test_summary_entries_skipped(self, incident_env):
        """摘要条目不作为分层依据——初诊 high + 摘要 medium → 仍用 high"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_skip_sum")
            _make_incident(
                db, root_cause="r", sufficiency_level="high",
                extra_diag=[
                    {"trigger": "summary", "root_cause": "r",
                     "at": datetime.now(), "sufficiency_level": "low"},
                ],
            )

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            assert len(tiers) == 1
            assert tiers[0]["sufficiency_level"] == "high", "摘要的 low 不应覆盖初诊 high"

    def test_no_sufficiency_level_falls_to_unknown(self, incident_env):
        """无 sufficiency_level 的诊断 → unknown 层"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_unknown")
            _make_incident(
                db, root_cause="r", sufficiency_level="",
                sufficiency_score=None,
            )

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            # 空 sufficiency_level 视为 unknown
            assert any(t["sufficiency_level"] == "unknown" for t in tiers)

    def test_date_window_filter(self, incident_env):
        """超出 days 的事故不统计"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_date")
            _make_incident(
                db, root_cause="r", sufficiency_level="high", days_ago=10,
            )
            _make_incident(
                db, root_cause="r", sufficiency_level="medium", days_ago=0,
            )

            resp = client.get(
                "/api/incidents/quality?days=1", headers=auth,
            )
            tiers = resp.json()
            levels = [t["sufficiency_level"] for t in tiers]
            assert "medium" in levels
            assert "high" not in levels, "10 天前的事故应被过滤"

    def test_resolve_rate_calculation(self, incident_env):
        """resolve_rate = resolved / count"""
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_resolve")
            _make_incident(
                db, root_cause="r", sufficiency_level="high", status="resolved",
            )
            _make_incident(
                db, root_cause="r", sufficiency_level="high", status="active",
            )

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = resp.json()
            assert len(tiers) == 1
            t = tiers[0]
            assert t["count"] == 2
            assert t["resolved"] == 1
            assert t["resolve_rate"] == 0.5

    def test_high_sufficiency_high_adoption_hypothesis(self, incident_env):
        """验证假设：high 层 ack_rate > low 层 ack_rate

        这是 D2 的核心价值——若高充分度诊断的采纳率反而更低，
        说明诊断质量与响应者信任存在断点（检索召回或表达问题）。
        """
        with TestClient(app) as client:
            db = incident_env
            auth = _auth(client, "qual_hypothesis")
            # high 层：3 事故全认领
            _make_incident(db, root_cause="r", sufficiency_level="high", acked_by="a")
            _make_incident(db, root_cause="r", sufficiency_level="high", acked_by="b")
            _make_incident(db, root_cause="r", sufficiency_level="high", acked_by="c")
            # low 层：3 事故全未认领
            _make_incident(db, root_cause="r", sufficiency_level="low", acked_by=None)
            _make_incident(db, root_cause="r", sufficiency_level="low", acked_by=None)
            _make_incident(db, root_cause="r", sufficiency_level="low", acked_by=None)

            resp = client.get("/api/incidents/quality", headers=auth)
            tiers = {t["sufficiency_level"]: t for t in resp.json()}
            assert tiers["high"]["ack_rate"] > tiers["low"]["ack_rate"], (
                "假设验证：高充分度应带来更高采纳率"
            )
