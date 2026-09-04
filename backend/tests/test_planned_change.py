"""
B5 维护窗口感知测试

覆盖：
1. _check_planned_change：有变更记录的服务 → True + 摘要
2. _check_planned_change：无变更记录的服务 → False + 空摘要
3. _check_planned_change：空服务名 → False
4. 新建事故 incident dict 含 planned_change/recent_changes 字段
5. 诊断 prompt 注入变更提示（planned_change=True 时）
6. 摘要 prompt 注入复盘变更提示
7. planned_change=False 时 prompt 不注入变更提示（无误报）
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.alerts import _build_rediagnosis_prompt, _build_summary_prompt, _check_planned_change


class TestCheckPlannedChange:
    """_check_planned_change 行为"""

    def test_service_with_changes_detected(self):
        """有变更记录的服务（payment-service）→ True + 摘要"""
        has_change, summary = _check_planned_change("payment-service")
        assert has_change is True
        assert summary != ""
        assert "deploy" in summary or "scale" in summary  # mock 数据含 deploy/scale

    def test_service_without_changes(self):
        """无变更记录的服务 → False + 空摘要"""
        has_change, summary = _check_planned_change("unknown-service-xyz")
        assert has_change is False
        assert summary == ""

    def test_empty_service(self):
        """空服务名 → False"""
        assert _check_planned_change("") == (False, "")
        assert _check_planned_change(None) == (False, "")


class TestIncidentPlannedChangeFields:
    """新建事故 incident dict 含 planned_change/recent_changes 字段

    通过 _check_planned_change 的返回值验证字段语义（不跑完整路由，避免依赖 DB）。
    """

    def test_payment_service_incident_marks_planned_change(self):
        """payment-service 事故应标记 planned_change=True（mock 变更台账有记录）"""
        has_change, summary = _check_planned_change("payment-service")
        # 模拟新建事故时的字段赋值
        incident = {
            "planned_change": has_change,
            "recent_changes": summary,
        }
        assert incident["planned_change"] is True
        assert incident["recent_changes"] != ""

    def test_unknown_service_incident_no_planned_change(self):
        """未知服务事故应标记 planned_change=False"""
        has_change, summary = _check_planned_change("nonexistent-service")
        incident = {
            "planned_change": has_change,
            "recent_changes": summary,
        }
        assert incident["planned_change"] is False
        assert incident["recent_changes"] == ""


class TestPromptInjection:
    """诊断/摘要 prompt 注入变更提示"""

    def test_rediagnosis_prompt_injects_change_hint(self):
        """planned_change=True → 重诊 prompt 含变更提示"""
        incident = {
            "incident_id": "INC-TEST",
            "alertnames": ["HighLatency"],
            "max_severity": "warning",
            "planned_change": True,
            "recent_changes": "[deploy] v2.3.1 发版（2026-08-02T14:20:00Z）",
            "diagnosis_history": [
                {"trigger": "initial", "root_cause": "慢查询", "confidence_level": "medium"}
            ],
        }
        prompt = _build_rediagnosis_prompt(incident, [])
        assert "计划内变更" in prompt
        assert "变更引发" in prompt
        assert "v2.3.1" in prompt

    def test_rediagnosis_prompt_no_hint_when_no_change(self):
        """planned_change=False → 重诊 prompt 不含变更提示（无误报）"""
        incident = {
            "incident_id": "INC-TEST",
            "alertnames": ["HighLatency"],
            "max_severity": "warning",
            "planned_change": False,
            "recent_changes": "",
            "diagnosis_history": [],
        }
        prompt = _build_rediagnosis_prompt(incident, [])
        assert "计划内变更" not in prompt

    def test_summary_prompt_injects_change_hint(self):
        """planned_change=True → 摘要 prompt 含复盘变更提示"""
        incident = {
            "incident_id": "INC-TEST",
            "alertnames": ["HighLatency"],
            "max_severity": "warning",
            "first_seen_at": None,
            "resolved_at": None,
            "planned_change": True,
            "recent_changes": "[scale] 连接池未调整",
            "diagnosis_history": [],
        }
        prompt = _build_summary_prompt(incident)
        assert "计划内变更" in prompt
        assert "变更引发" in prompt
        assert "连接池" in prompt

    def test_summary_prompt_no_hint_when_no_change(self):
        """planned_change=False → 摘要 prompt 不含变更提示"""
        incident = {
            "incident_id": "INC-TEST",
            "alertnames": ["HighLatency"],
            "max_severity": "warning",
            "first_seen_at": None,
            "resolved_at": None,
            "planned_change": False,
            "recent_changes": "",
            "diagnosis_history": [],
        }
        prompt = _build_summary_prompt(incident)
        assert "计划内变更" not in prompt
