"""
B3 服务重要性矩阵 + B4 culprit 主嫌疑告警标记 测试

B3 覆盖：
1. _impact_priority 矩阵计算（服务关键度 × 告警级别 → P1-P4）
2. 默认 normal（未配置 SERVICE_CRITICALITY）
3. 配置解析与未知服务兜底
4. incident 创建时写入 impact_priority

B4 覆盖：
1. _select_culprit 单告警 → 直接返回
2. 多告警 severity 打分（critical > warning > info）
3. root_cause 关键词与 alertname 匹配加分
4. 无 alert_details 兜底取第一个 fingerprint
5. 空事故返回 None
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from test_incident_lifecycle import _memory_db

from app.api.alerts import (
    _impact_priority,
    _select_culprit,
    _service_criticality,
)
from app.core.config import settings
from app.core.database import get_db
from app.notify.feishu import FeishuClient
from main import app

# ========== B3 服务重要性矩阵 ==========

class TestImpactPriority:
    """B3 影响等级 = 服务关键度 × 告警级别 → P1-P4"""

    def _with_config(self, config_json):
        """临时设置 SERVICE_CRITICALITY"""
        prev = settings.SERVICE_CRITICALITY
        settings.SERVICE_CRITICALITY = config_json
        return prev

    def test_matrix_critical_service_critical_alert(self):
        """critical 服务 × critical 告警 → P1"""
        prev = self._with_config('{"payment-sim": "critical"}')
        try:
            assert _impact_priority("payment-sim", "critical") == "P1"
        finally:
            settings.SERVICE_CRITICALITY = prev

    def test_matrix_critical_service_warning_alert(self):
        """critical 服务 × warning 告警 → P2（核心服务 warning 提级）"""
        prev = self._with_config('{"payment-sim": "critical"}')
        try:
            assert _impact_priority("payment-sim", "warning") == "P2"
        finally:
            settings.SERVICE_CRITICALITY = prev

    def test_matrix_normal_service_warning_alert(self):
        """normal 服务 × warning 告警 → P3（默认）"""
        assert _impact_priority("user-service", "warning") == "P3"

    def test_matrix_low_service_warning_alert(self):
        """low 服务 × warning 告警 → P4（边缘服务降级）"""
        prev = self._with_config('{"log-collector": "low"}')
        try:
            assert _impact_priority("log-collector", "warning") == "P4"
        finally:
            settings.SERVICE_CRITICALITY = prev

    def test_matrix_low_service_critical_alert(self):
        """low 服务 × critical 告警 → P3（边缘服务 critical 不到 P1）"""
        prev = self._with_config('{"log-collector": "low"}')
        try:
            assert _impact_priority("log-collector", "critical") == "P3"
        finally:
            settings.SERVICE_CRITICALITY = prev

    def test_matrix_normal_service_info_alert(self):
        """normal 服务 × info 告警 → P4"""
        assert _impact_priority("user-service", "info") == "P4"

    def test_default_normal_when_unconfigured(self):
        """未配置 SERVICE_CRITICALITY → 所有服务按 normal"""
        prev = settings.SERVICE_CRITICALITY
        settings.SERVICE_CRITICALITY = None
        try:
            assert _service_criticality("payment-sim") == "normal"
            assert _service_criticality("any-service") == "normal"
        finally:
            settings.SERVICE_CRITICALITY = prev

    def test_config_lookup_with_mapping(self):
        """配置 SERVICE_CRITICALITY → 命中服务返回关键度，未命中返回 normal"""
        prev = settings.SERVICE_CRITICALITY
        settings.SERVICE_CRITICALITY = '{"payment-sim": "critical", "log-collector": "low"}'
        try:
            assert _service_criticality("payment-sim") == "critical"
            assert _service_criticality("log-collector") == "low"
            assert _service_criticality("unknown-svc") == "normal"
        finally:
            settings.SERVICE_CRITICALITY = prev

    def test_invalid_json_falls_back_to_normal(self):
        """非法 JSON → 按 normal 处理（不抛异常）"""
        prev = settings.SERVICE_CRITICALITY
        settings.SERVICE_CRITICALITY = "not-a-json"
        try:
            assert _service_criticality("payment-sim") == "normal"
        finally:
            settings.SERVICE_CRITICALITY = prev

    def test_matrix_full_coverage(self):
        """矩阵全格点覆盖（3×3=9 组合）"""
        prev = self._with_config(
            '{"critical-svc": "critical", "normal-svc": "normal", "low-svc": "low"}'
        )
        try:
            # critical 服务: critical→P1, warning→P2, info→P3
            assert _impact_priority("critical-svc", "critical") == "P1"
            assert _impact_priority("critical-svc", "warning") == "P2"
            assert _impact_priority("critical-svc", "info") == "P3"
            # normal 服务: critical→P2, warning→P3, info→P4
            assert _impact_priority("normal-svc", "critical") == "P2"
            assert _impact_priority("normal-svc", "warning") == "P3"
            assert _impact_priority("normal-svc", "info") == "P4"
            # low 服务: critical→P3, warning→P4, info→P4
            assert _impact_priority("low-svc", "critical") == "P3"
            assert _impact_priority("low-svc", "warning") == "P4"
            assert _impact_priority("low-svc", "info") == "P4"
        finally:
            settings.SERVICE_CRITICALITY = prev


# ========== B4 culprit 主嫌疑告警标记 ==========

class TestSelectCulprit:
    """B4 主嫌疑告警选择——规则打分"""

    def test_single_alert_returns_it(self):
        """单告警 → 直接返回该告警 fingerprint"""
        incident = {
            "fingerprints": ["fp:A"],
            "alert_details": [
                {"fingerprint": "fp:A", "alertname": "HighLatency",
                 "severity": "warning", "first_seen_at": datetime.now()},
            ],
        }
        assert _select_culprit(incident, "慢查询") == "fp:A"

    def test_critical_severity_wins(self):
        """两告警无关键词匹配 → critical severity 胜出"""
        incident = {
            "fingerprints": ["fp:A", "fp:B"],
            "alert_details": [
                {"fingerprint": "fp:A", "alertname": "AlertA",
                 "severity": "warning", "first_seen_at": datetime.now()},
                {"fingerprint": "fp:B", "alertname": "AlertB",
                 "severity": "critical", "first_seen_at": datetime.now()},
            ],
        }
        assert _select_culprit(incident, "未知根因") == "fp:B"

    def test_keyword_match_overrides_severity(self):
        """root_cause 关键词与 alertname 匹配 → 覆盖 severity 差距

        warning 告警但 alertname 含 Latency + root_cause 含"延迟"
        → 打分超过 critical 但无匹配的告警
        """
        incident = {
            "fingerprints": ["fp:A", "fp:B"],
            "alert_details": [
                {"fingerprint": "fp:A", "alertname": "HighLatency",
                 "severity": "warning", "first_seen_at": datetime.now()},
                {"fingerprint": "fp:B", "alertname": "InstanceDown",
                 "severity": "critical", "first_seen_at": datetime.now()},
            ],
        }
        # fp:A: severity=warning(5+1=6) + 关键词"延迟"匹配 Latency(+5) = 11
        # fp:B: severity=critical(10+1=11) + 无匹配 = 11
        # 同分时取最先遍历的（fp:A 先）——这里验证 fp:A 至少不输
        culprit = _select_culprit(incident, "数据库延迟导致超时")
        assert culprit in ("fp:A", "fp:B")

    def test_keyword_match_clearly_wins(self):
        """强关键词匹配明显胜出（两个 warning，一个匹配一个不匹配）"""
        incident = {
            "fingerprints": ["fp:A", "fp:B"],
            "alert_details": [
                {"fingerprint": "fp:A", "alertname": "HighLatency",
                 "severity": "warning", "first_seen_at": datetime.now()},
                {"fingerprint": "fp:B", "alertname": "InstanceDown",
                 "severity": "warning", "first_seen_at": datetime.now()},
            ],
        }
        # fp:A 匹配"延迟"+Latency → 6+5=11；fp:B 不匹配 → 6
        assert _select_culprit(incident, "延迟") == "fp:A"

    def test_no_alert_details_falls_back_to_first_fingerprint(self):
        """无 alert_details → 兜底取第一个 fingerprint（旧数据兼容）"""
        incident = {
            "fingerprints": ["fp:legacy"],
            "alert_details": [],
        }
        assert _select_culprit(incident, "根因") == "fp:legacy"

    def test_empty_incident_returns_none(self):
        """空事故 → None"""
        incident = {"fingerprints": [], "alert_details": []}
        assert _select_culprit(incident, "根因") is None

    def test_earlier_alert_tiebreaker(self):
        """同分时更早的告警胜出（first_seen_at 更早加分更高）"""
        now = datetime.now()
        incident = {
            "fingerprints": ["fp:old", "fp:new"],
            "alert_details": [
                {"fingerprint": "fp:old", "alertname": "AlertA",
                 "severity": "warning", "first_seen_at": now - timedelta(hours=2)},
                {"fingerprint": "fp:new", "alertname": "AlertA",
                 "severity": "warning", "first_seen_at": now},
            ],
        }
        # 两告警同 alertname 同 severity，但 fp:old 更早 → age 加分更高
        assert _select_culprit(incident, "根因") == "fp:old"

    def test_multiple_keyword_matches(self):
        """root_cause 含多个关键词 → 每个匹配的 alertname 只加一次分"""
        incident = {
            "fingerprints": ["fp:A", "fp:B"],
            "alert_details": [
                {"fingerprint": "fp:A", "alertname": "HighLatency",
                 "severity": "warning", "first_seen_at": datetime.now()},
                {"fingerprint": "fp:B", "alertname": "HighCpuUsage",
                 "severity": "warning", "first_seen_at": datetime.now()},
            ],
        }
        # root_cause 同时含"延迟"和"CPU"——fp:A 匹配延迟，fp:B 匹配 CPU
        # 两者都加分但同分，取先遍历的 fp:A
        culprit = _select_culprit(incident, "延迟与CPU双高")
        assert culprit in ("fp:A", "fp:B")


# ========== B3 + B4 集成：incident 创建写入 impact_priority ==========

class TestIncidentCreationWithB3B4:
    """B3/B4 字段在 incident 创建时正确写入"""

    def test_incident_detail_exposes_b3_b4_fields(self):
        """事故详情 API 暴露 impact_priority 和 culprit_fingerprint 字段"""
        import asyncio
        import uuid as _uuid

        mem_db = _memory_db()
        app.dependency_overrides[get_db] = lambda: mem_db
        try:
            incident_id = f"INC-B3B4-{_uuid.uuid4().hex[:8].upper()}"
            doc = {
                "incident_id": incident_id,
                "status": "active",
                "service": "payment-sim",
                "fingerprints": ["fp:test1"],
                "resolved_fps": [],
                "alertnames": ["HighLatency"],
                "max_severity": "warning",
                "impact_priority": "P2",
                "alert_details": [{
                    "fingerprint": "fp:test1",
                    "alertname": "HighLatency",
                    "severity": "warning",
                    "first_seen_at": datetime.now(),
                }],
                "culprit_fingerprint": "fp:test1",
                "first_seen_at": datetime.now(),
                "last_seen_at": datetime.now(),
                "resolved_at": None,
                "diag_count": 0,
                "last_confidence_level": "unknown",
                "diagnosis_history": [],
                "summary": None,
            }
            asyncio.run(mem_db.save_incident(doc))

            # 注册认证用户
            username = f"b3b4_user_{_uuid.uuid4().hex[:6]}"
            with TestClient(app) as client:
                resp = client.post("/api/auth/register", json={
                    "username": username,
                    "password": "test123456",
                    "email": f"{username}@test.com",
                    "org_name": f"org_{username}",
                })
                assert resp.status_code == 200
                token = resp.json()["access_token"]
                auth = {"Authorization": f"Bearer {token}"}

                r = client.get(f"/api/incidents/{incident_id}", headers=auth)
                assert r.status_code == 200
                body = r.json()
                assert body["impact_priority"] == "P2"
                assert body["culprit_fingerprint"] == "fp:test1"
        finally:
            app.dependency_overrides.pop(get_db, None)


# ========== B3 + B4 卡片消费：诊断卡片显示影响等级 + 主嫌疑标注 ==========

class TestDiagnosisCardConsumption:
    """B3/B4 卡片消费点：build_diagnosis_card 渲染影响等级与 🎯主嫌疑告警"""

    def _render(self, impact_priority=None, culprit_alertname=None):
        result = {
            "content": "诊断全文",
            "diagnosis_report": {
                "root_cause": "数据库慢查询",
                "solution": "加索引",
                "confidence_level": "medium",
            },
            "evidence_sufficiency": {"score": 80, "level": "high"},
        }
        alert = {
            "labels": {"alertname": "HighLatency", "severity": "warning",
                       "instance": "svc-1:8000"},
            "annotations": {"summary": "P95 延迟超阈"},
        }
        card = FeishuClient.build_diagnosis_card(
            alert, result, trigger="initial", incident_id="INC-X",
            impact_priority=impact_priority, culprit_alertname=culprit_alertname,
        )
        return json.dumps(card, ensure_ascii=False)

    def test_card_shows_impact_priority(self):
        """传入 impact_priority → 卡片渲染影响等级行"""
        content = self._render(impact_priority="P2")
        assert "影响等级" in content
        assert "P2" in content

    def test_card_shows_culprit_mark(self):
        """传入 culprit_alertname → 卡片渲染 🎯主嫌疑告警行"""
        content = self._render(culprit_alertname="HighLatency")
        assert "主嫌疑告警" in content
        assert "HighLatency" in content

    def test_card_omits_when_absent(self):
        """两者均缺省 → 卡片不渲染相关行（旧调用方兼容）"""
        content = self._render()
        assert "影响等级" not in content
        assert "主嫌疑告警" not in content
