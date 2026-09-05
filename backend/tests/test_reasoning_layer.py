"""
推理层增强测试

覆盖：
1. 服务依赖拓扑工具：数据文件加载、方向过滤、未知服务兜底、MCP 不剔除
2. 诊断 prompt：变更归因纪律、冲突处理、跨服务排查关键词
3. 证据充分度：因子计分、降级封顶、level 分级
4. 知识新鲜度：过期判定、软降权、引用过期标记、frontmatter 字段

全部确定性运行。
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))




# ============================================================
# 1. 服务依赖拓扑工具
# ============================================================

class TestServiceDependencies:

    def test_known_service_full_topology(self):
        """已知服务返回下游 + 上游（与种子事故对齐：payment-service 依赖 mysql）"""
        from app.langgraph_agent.tools import get_service_dependencies
        data = json.loads(get_service_dependencies.invoke({"service": "payment-service"}))
        assert "mysql" in data["depends_on"], "拓扑应与慢查询根因场景对齐"
        assert "order-service" in data["depends_on"]
        assert data["called_by"], "上游调用方应非空"

    def test_direction_filter(self):
        from app.langgraph_agent.tools import get_service_dependencies
        down = json.loads(get_service_dependencies.invoke({
            "service": "payment-service", "direction": "downstream"}))
        assert down["depends_on"]
        assert "called_by" not in down

        up = json.loads(get_service_dependencies.invoke({
            "service": "payment-service", "direction": "upstream"}))
        assert up["called_by"]
        assert "depends_on" not in up

    def test_unknown_service_empty_with_note(self):
        """未知服务返回空列表 + 说明（宁缺勿错，不编造拓扑）"""
        from app.langgraph_agent.tools import get_service_dependencies
        data = json.loads(get_service_dependencies.invoke({"service": "no-such-svc"}))
        assert data["depends_on"] == []
        assert "无法跨服务排查" in data["note"]

    def test_not_removed_by_mcp_merge(self):
        """拓扑工具不在 _MOCK_MONITORING_TOOLS 中（MCP 启用时仍存活）"""
        from app.langgraph_agent.agent import LangGraphAgent
        assert "get_service_dependencies" not in getattr(
            LangGraphAgent, "_MOCK_MONITORING_TOOLS", set())

    def test_topology_file_exists_and_aligned(self):
        """拓扑数据文件存在且 payment-service 与 mysql 有边（跨服务场景可用）"""
        path = os.path.join(os.path.dirname(__file__), "..", "data", "service_topology.json")
        assert os.path.exists(path), "拓扑数据文件应存在（数据与逻辑分离）"
        with open(path, "r", encoding="utf-8") as f:
            topo = json.load(f)
        assert "mysql" in topo["payment-service"]["depends_on"]


# ============================================================
# 2. 诊断 prompt 关键约束
# ============================================================

class TestPromptConstraints:

    @staticmethod
    def _prompt() -> str:
        from app.langgraph_agent.agent import LangGraphAgent
        agent = LangGraphAgent.__new__(LangGraphAgent)
        return agent._build_diagnosis_prompt()

    def test_change_attribution_discipline(self):
        """变更锚定防线：因果论证义务 + 无关变更显式区分"""
        prompt = self._prompt()
        assert "因果论证义务" in prompt
        assert "同时发生的无关变更" in prompt
        assert "早于" in prompt  # 变更时间早于故障起点

    def test_conflict_handling_instruction(self):
        """历史结论冲突：优先复盘日期更新者 + 注明冲突"""
        prompt = self._prompt()
        assert "冲突" in prompt
        assert "effective_date" in prompt

    def test_cross_service_guidance(self):
        """跨服务排查：拓扑工具指引 + 补充取证 + 因果链"""
        prompt = self._prompt()
        assert "get_service_dependencies" in prompt
        assert "补充取证" in prompt
        assert "依赖拓扑" in prompt


# ============================================================
# 3. 证据充分度
# ============================================================

class TestEvidenceSufficiency:

    def test_full_evidence_high_score(self):
        """监控 + 知识库 + 变更 + 拓扑 + 完整报告 → 高分"""
        from app.langgraph_agent.evidence import _compute_evidence_sufficiency
        suff = _compute_evidence_sufficiency(
            monitoring_evidence=[{"type": "metrics"}, {"type": "logs"}],
            citations=[{"doc_id": "1"}, {"doc_id": "2"}],
            tools_used=["query_metrics", "get_recent_changes", "get_service_dependencies"],
            diagnosis_report={"root_cause": "x", "solution": "y"},
        )
        assert suff["score"] == 100
        assert suff["level"] == "high"
        assert suff["factors"]["monitoring"]["score"] == 30
        assert suff["factors"]["knowledge"]["score"] == 25

    def test_no_evidence_low_score(self):
        """无监控、无引用、无工具 → 低分"""
        from app.langgraph_agent.evidence import _compute_evidence_sufficiency
        suff = _compute_evidence_sufficiency(
            monitoring_evidence=[], citations=[], tools_used=[],
            diagnosis_report={"root_cause": "x"},
        )
        # 仅报告不完整分 10
        assert suff["score"] == 10
        assert suff["level"] == "low"

    def test_mcp_degraded_caps_at_50(self):
        """监控源降级 → 总分封顶 50（prompt 硬约束的数值化）"""
        from app.langgraph_agent.evidence import _compute_evidence_sufficiency
        suff = _compute_evidence_sufficiency(
            monitoring_evidence=[{"type": "metrics"}],
            citations=[{"doc_id": "1"}, {"doc_id": "2"}],
            tools_used=["query_metrics", "get_recent_changes"],
            diagnosis_report={"root_cause": "x", "solution": "y"},
            mcp_degraded=True,
        )
        assert suff["score"] == 50
        assert suff["factors"]["mcp_degraded"]["capped"] is True
        assert suff["level"] == "medium"

    def test_partial_monitoring_types(self):
        """只有一类监控证据 → 15 分"""
        from app.langgraph_agent.evidence import _compute_evidence_sufficiency
        suff = _compute_evidence_sufficiency(
            monitoring_evidence=[{"type": "logs"}],
            citations=[], tools_used=[], diagnosis_report=None,
        )
        assert suff["factors"]["monitoring"]["score"] == 15
        assert suff["factors"]["monitoring"]["types"] == ["logs"]

    def test_level_thresholds(self):
        from app.langgraph_agent.evidence import _compute_evidence_sufficiency
        assert _compute_evidence_sufficiency(
            [], [], [], None)["level"] == "low"
        # 40 分边界：monitoring 30 + kb 12(1条引用) 不足；构造 25+15=40
        suff = _compute_evidence_sufficiency(
            monitoring_evidence=[{"type": "metrics"}, {"type": "logs"}],
            citations=[], tools_used=["get_recent_changes"], diagnosis_report=None,
        )
        assert suff["score"] == 45 and suff["level"] == "medium"


# ============================================================
# 4. 知识新鲜度
# ============================================================

class TestFreshness:

    def test_is_expired_variants(self):
        from app.knowledge.unified_store import UnifiedKnowledgeStore as UKS
        now = datetime(2026, 8, 31)
        assert UKS._is_expired({"valid_until": "2026-08-01"}, now=now) is True
        assert UKS._is_expired({"valid_until": "2026-12-31"}, now=now) is False
        assert UKS._is_expired({}, now=now) is False, "无 valid_until 视为长期有效"
        assert UKS._is_expired({"valid_until": "not-a-date"}, now=now) is False, "非法格式不误杀"
        assert UKS._is_expired({}, now=now) is False

    def test_freshness_decay(self):
        """过期文档分数 ×0.5 + 打 _expired 标记；有效/无期限文档不变"""
        from app.knowledge.unified_store import UnifiedKnowledgeStore as UKS
        now = datetime(2026, 8, 31)
        results = [
            {"id": "a", "score": 0.8, "metadata": {"valid_until": "2026-01-01"}},   # 过期
            {"id": "b", "score": 0.6, "metadata": {"valid_until": "2027-01-01"}},   # 有效
            {"id": "c", "score": 0.5, "metadata": {}},                               # 无期限
        ]
        UKS._apply_freshness_decay(results, now=now)
        assert abs(results[0]["score"] - 0.4) < 1e-9
        assert results[0]["metadata"]["_expired"] is True
        assert results[1]["score"] == 0.6 and "_expired" not in results[1]["metadata"]
        assert results[2]["score"] == 0.5

    def test_decay_improves_fresh_ranking(self):
        """过期高分文档衰减后让位给有效文档（软降权的排序效果）"""
        from app.knowledge.unified_store import UnifiedKnowledgeStore as UKS
        now = datetime(2026, 8, 31)
        results = [
            {"id": "expired", "score": 0.9, "metadata": {"valid_until": "2026-01-01"}},
            {"id": "fresh", "score": 0.7, "metadata": {}},
        ]
        UKS._apply_freshness_decay(results, now=now)
        results.sort(key=lambda x: x["score"], reverse=True)
        assert results[0]["id"] == "fresh"

    def test_frontmatter_valid_until_extracted(self):
        """valid_until/effective_date 随 BUSINESS_FIELDS 提取"""
        from app.document.frontmatter import extract_business_metadata
        meta = extract_business_metadata({
            "doc_type": "incident", "service": "payment-service",
            "valid_until": "2027-12-31", "effective_date": "2026-08-31",
        })
        assert meta["valid_until"] == "2027-12-31"
        assert meta["effective_date"] == "2026-08-31"

    def test_citation_expired_flag(self):
        """引用列表携带过期标记（artifact 的 metadata._expired 透传）"""
        from app.langgraph_agent.evidence import _build_citations
        docs = [
            {"doc_id": "d1", "title": "旧 SOP", "metadata": {"_expired": True}},
            {"doc_id": "d2", "title": "新 SOP", "metadata": {}},
        ]
        citations = _build_citations(docs)
        assert citations[0]["expired"] is True
        assert citations[1]["expired"] is False
