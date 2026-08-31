"""
业务层改造测试：告警自动诊断闭环 + 业务工具 + 知识库生命周期

覆盖：
1. 告警自动诊断：冷却占坑、firing 过滤、开关、诊断卡片构建、端到端 mock 流程
2. 新增工具：query_metrics 时间窗、get_recent_changes、create_incident_ticket
3. 变更证据解析：_parse_changes_evidence
4. 知识库生命周期：自动分类兜底、审计日志（内存降级）、负反馈标记

所有测试确定性运行，不依赖 LLM / Redis / 真实飞书。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

# ============================================================
# 1. 告警自动诊断
# ============================================================
# 冷却与触发逻辑已迁移到 Incident 生命周期模型（见 test_incident_lifecycle.py），
# 此处保留诊断卡片构建测试。


class TestDiagnosisCard:
    """诊断报告卡片构建"""

    def _result(self, with_report=True):
        if with_report:
            return {
                "content": "### 根因分析\nfull text",
                "tools_used": ["query_metrics"],
                "diagnosis_report": {
                    "root_cause": "连接池耗尽",
                    "solution": "扩容 + 治理慢查询",
                    "confidence": "高",
                    "confidence_level": "high",
                },
            }
        return {"content": "### 现象\n纯文本降级", "tools_used": [], "diagnosis_report": None}

    def test_card_with_report(self):
        """有结构化报告：显示根因/置信度"""
        from app.notify.feishu import FeishuClient
        card = FeishuClient.build_diagnosis_card({"labels": {"alertname": "A"}}, self._result())
        assert card["header"]["template"] == "orange"
        body = json.dumps(card["elements"], ensure_ascii=False)
        assert "连接池耗尽" in body
        assert "高" in body
        assert "🤖" in body  # 人工确认提示

    def test_card_without_report_falls_back_to_content(self):
        """无结构化报告：降级为全文截断"""
        from app.notify.feishu import FeishuClient
        card = FeishuClient.build_diagnosis_card({"labels": {"alertname": "A"}}, self._result(False))
        body = json.dumps(card["elements"], ensure_ascii=False)
        assert "纯文本降级" in body

    def test_card_truncates_long_content(self):
        """超长内容被截断"""
        from app.notify.feishu import FeishuClient
        result = {"content": "x" * 5000, "tools_used": [], "diagnosis_report": None}
        card = FeishuClient.build_diagnosis_card({"labels": {"alertname": "A"}}, result)
        body = json.dumps(card["elements"], ensure_ascii=False)
        assert "(截断)" in body


# ============================================================
# 2. 业务工具
# ============================================================

class TestBusinessTools:
    """query_metrics 时间窗 / get_recent_changes / create_incident_ticket"""

    def test_query_metrics_short_window_incident_values(self):
        """短窗口返回故障时刻瞬时值"""
        from app.langgraph_agent.tools import query_metrics
        data = json.loads(query_metrics.invoke({"service": "payment-service", "time_range": "30m"}))
        assert data["time_range"] == "30m"
        assert data["metrics"]["error_rate"]["value"] == 0.38

    def test_query_metrics_long_window_averaged(self):
        """长窗口返回均值（故障被稀释）"""
        from app.langgraph_agent.tools import query_metrics
        data = json.loads(query_metrics.invoke({"service": "payment-service", "time_range": "24h"}))
        assert data["metrics"]["error_rate"]["value"] < 0.38

    def test_get_recent_changes_payment_service(self):
        """payment-service 有与种子事故对齐的变更事件"""
        from app.langgraph_agent.tools import get_recent_changes
        data = json.loads(get_recent_changes.invoke({"service": "payment-service", "hours": 24}))
        assert data["count"] >= 1
        deploy_events = [c for c in data["changes"] if c["type"] == "deploy"]
        assert deploy_events, "应包含发版事件（变更先于深挖的核心场景）"

    def test_get_recent_changes_unknown_service(self):
        """未知服务返回空列表（宁缺勿错）"""
        from app.langgraph_agent.tools import get_recent_changes
        data = json.loads(get_recent_changes.invoke({"service": "no-such-svc"}))
        assert data["count"] == 0
        assert data["changes"] == []

    def test_create_incident_ticket_returns_ticket(self):
        """工单工具返回 TK- 编号与状态"""
        from app.langgraph_agent.tools import create_incident_ticket
        data = json.loads(create_incident_ticket.invoke({
            "service": "payment-service",
            "title": "连接池耗尽",
            "root_cause": "慢查询放大连接需求" * 50,  # 超长截断
            "severity": "P1",
        }))
        assert data["ticket_id"].startswith("TK-")
        assert data["status"] == "created"
        assert len(data["root_cause"]) <= 200
        assert "演示环境" in data["note"]

    def test_new_tools_not_removed_by_mcp_merge(self):
        """新工具不在 _MOCK_MONITORING_TOOLS 中（MCP 启用时仍存活）"""
        from app.langgraph_agent.agent import LangGraphAgent
        # 该集合只包含会被 MCP 剔除的本地 mock 监控工具
        assert "get_recent_changes" not in getattr(LangGraphAgent, "_MOCK_MONITORING_TOOLS", set())
        assert "create_incident_ticket" not in getattr(LangGraphAgent, "_MOCK_MONITORING_TOOLS", set())


class TestChangesEvidence:
    """变更事件证据解析"""

    def test_parse_changes_evidence_with_events(self):
        from app.langgraph_agent.agent import LangGraphAgent
        data = {
            "service": "payment-service", "hours": 2, "count": 1,
            "changes": [{"change_id": "CHG-1", "type": "deploy",
                         "time": "2026-08-02T14:20:00Z", "description": "v2.3.1 发版"}],
        }
        ev = LangGraphAgent._parse_changes_evidence(data)
        assert ev["type"] == "changes"
        assert ev["service"] == "payment-service"
        assert "发版" in ev["summary"] or "deploy" in ev["summary"]

    def test_parse_changes_evidence_empty(self):
        from app.langgraph_agent.agent import LangGraphAgent
        ev = LangGraphAgent._parse_changes_evidence({"service": "x", "hours": 24, "changes": []})
        assert ev["type"] == "changes"
        assert "无变更" in ev["summary"]


# ============================================================
# 3. 知识库生命周期
# ============================================================

class TestAutoClassification:
    """无 frontmatter 自动分类兜底"""

    def test_infer_incident_from_filename(self):
        from app.document.frontmatter import infer_business_metadata
        meta = infer_business_metadata("INC-2026-001 payment-service 故障处理.md")
        assert meta["doc_type"] == "incident"
        assert meta["service"] == "payment-service"
        assert meta["source"] == "auto_inferred"

    def test_infer_postmortem(self):
        from app.document.frontmatter import infer_business_metadata
        meta = infer_business_metadata("mysql-slow-query-postmortem.md")
        assert meta["doc_type"] == "postmortem"

    def test_infer_sop(self):
        from app.document.frontmatter import infer_business_metadata
        meta = infer_business_metadata("redis-内存告警-处置预案.md", "")
        assert meta["doc_type"] == "sop"

    def test_infer_default_manual(self):
        """运维关键词兜底为 manual"""
        from app.document.frontmatter import infer_business_metadata
        meta = infer_business_metadata("kafka-gateway-运维手册.md")
        assert meta["doc_type"] == "manual"

    def test_infer_no_service_no_fabrication(self):
        """无高置信 service 命中 → 不编造"""
        from app.document.frontmatter import infer_business_metadata
        meta = infer_business_metadata("random-notes.txt", "", "随便记点什么")
        assert "service" not in meta


class TestAuditAndFeedback:
    """工具审计日志 + 负反馈标记（内存降级模式，不依赖 Mongo）"""

    from app.core.database import Database  # 类型注解引用


    @staticmethod
    def _memory_db() -> "Database":
        """构造强制内存模式的 Database（本地 Mongo 可用时 connect() 会切到 Mongo 分支）"""
        from app.core.database import Database

        db = Database()

        async def _no_connect():
            return None

        db.connect = _no_connect
        db._use_mongo = False
        return db

    @pytest.mark.asyncio
    async def test_save_tool_audit_log_memory_fallback(self):
        db = self._memory_db()
        entry = {
            "audit_id": "audit_test1",
            "tool_name": "query_metrics",
            "tool_args": '{"service": "payment-service"}',
            "user_id": "u1", "session_id": "s1", "intent": "diagnosis",
        }
        await db.save_tool_audit_log(entry)
        assert len(db._tool_audit_logs) == 1
        assert db._tool_audit_logs[0]["tool_name"] == "query_metrics"
        assert "created_at" in db._tool_audit_logs[0]

    @pytest.mark.asyncio
    async def test_mark_documents_for_review_memory_fallback(self):
        db = self._memory_db()
        marked = await db.mark_documents_for_review(
            ["doc_a", "doc_b"], reason="negative_feedback", feedback_id="fb_1",
        )
        assert marked == 2

    @pytest.mark.asyncio
    async def test_mark_documents_empty_list(self):
        db = self._memory_db()
        assert await db.mark_documents_for_review([]) == 0

    def test_feedback_request_accepts_document_ids(self):
        """FeedbackRequest 兼容前端暂未传 document_ids 的场景"""
        from app.api.langgraph import FeedbackRequest
        req = FeedbackRequest(session_id="s", message_content="m", rating="negative")
        assert req.document_ids is None
        req2 = FeedbackRequest(session_id="s", message_content="m", rating="negative",
                               document_ids=["doc_1"])
        assert req2.document_ids == ["doc_1"]
