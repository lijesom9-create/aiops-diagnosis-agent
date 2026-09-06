"""飞轮效果度量单元测试（批次 E）。

验证：恢复摘要自动沉淀的知识（metadata.auto_ingested）在检索命中时
产生 ops_flywheel_* 指标，且 artifact 透出该标记。
"""
import pytest

from app.observability.metrics import get_metrics


class _FakeStore:
    """可编程知识库桩：返回预设检索结果（不落向量库）"""

    def __init__(self, results):
        self._results = results
        self.calls = 0

    def hybrid_search_parent_child(self, query, **kwargs):
        self.calls += 1
        return list(self._results)

    def delete_by_document(self, document_id):
        return True


@pytest.fixture(autouse=True)
def _clean_metrics():
    get_metrics().reset()
    yield
    get_metrics().reset()


def test_ingest_builds_parent_child_with_marker():
    """恢复摘要入库必须产出父子两条目，且带 auto_ingested 飞轮标记

    （历史缺陷：单条目无 chunk_type 被分进子块库，父块库永远没有它，
    父子分离检索的取回阶段拿不到文档——飞轮回流知识永远检索不到）"""
    from app.knowledge.incident_ingest import _build_knowledge_items

    items = _build_knowledge_items(
        incident={
            "incident_id": "INC-TEST-001", "service": "payment-service",
            "alertnames": ["HighCPU"], "max_severity": "critical",
            "diagnosis_history": [{"trigger": "initial", "sufficiency_level": "high"}],
        },
        report={"root_cause": "连接池耗尽", "confidence_level": "high"},
        action_items=[{"item": "扩容连接池", "owner": "ops"}],
        summary="凌晨批量任务抢占连接",
        valid_days=90,
    )
    assert len(items) == 2
    parent, child = items
    assert parent.id == "incident_INC-TEST-001"
    assert parent.metadata["chunk_type"] == "parent"
    assert parent.metadata["auto_ingested"] is True
    assert parent.metadata["document_id"] == "incident_INC-TEST-001"
    assert child.metadata["chunk_type"] == "child"
    assert child.metadata["parent_id"] == "incident_INC-TEST-001"
    assert child.metadata["auto_ingested"] is True


@pytest.mark.asyncio
async def test_search_counts_flywheel_hits():
    """命中自动沉淀知识 → hit_docs 计数 + searches {auto_hit: true} + artifact 透出"""
    from app.langgraph_agent import retrieval_context
    from app.langgraph_agent.retrieval_context import pop_retrieval_buffer  # noqa: F401
    from app.langgraph_agent.tools_retrieval import search_knowledge

    store = _FakeStore([
        {"title": "[事故复盘] payment-service - 连接池耗尽", "content": "复盘内容",
         "score": 0.9, "metadata": {"auto_ingested": True, "doc_type": "incident",
                                    "service": "payment-service", "document_id": "incident_INC-1"}},
        {"title": "人工上传的运维手册", "content": "手册内容",
         "score": 0.8, "metadata": {"doc_type": "manual", "service": "payment-service",
                                    "document_id": "doc_manual1"}},
    ])
    retrieval_context.set_knowledge_store(store)
    try:
        # graph 语境（state + tool_call_id 注入）→ 返回 Command 把结果写进 state
        result = search_knowledge.func(
            query=f"支付服务故障复盘-{id(store)}",
            state={"retrieved_docs": []},
            tool_call_id="call_t1",
        )
        from langgraph.types import Command
        assert isinstance(result, Command)
        docs = result.update["retrieved_docs"]
        assert len(docs) == 2
        tm = result.update["messages"][0]
        assert "[事故复盘]" in tm.content and tm.tool_call_id == "call_t1"
        assert (get_metrics().get_metric("ops_flywheel_hit_docs_total",
                                         {"doc_type": "incident"})["value"] == 1)
        assert (get_metrics().get_metric("ops_flywheel_searches_total",
                                         {"auto_hit": "true"})["value"] == 1)
        flags = {d["title"]: d["auto_ingested"] for d in docs}
        assert flags["[事故复盘] payment-service - 连接池耗尽"] is True
        assert flags["人工上传的运维手册"] is False
    finally:
        retrieval_context.set_knowledge_store(None)


@pytest.mark.asyncio
async def test_search_without_auto_hits_labels_false():
    """无自动沉淀知识命中 → searches {auto_hit: false}，hit_docs 不产生"""
    from app.langgraph_agent import retrieval_context
    from app.langgraph_agent.retrieval_context import pop_retrieval_buffer  # noqa: F401
    from app.langgraph_agent.tools_retrieval import search_knowledge

    store = _FakeStore([
        {"title": "纯手册", "content": "内容", "score": 0.7,
         "metadata": {"doc_type": "manual", "document_id": "doc_m2"}},
    ])
    retrieval_context.set_knowledge_store(store)
    try:
        search_knowledge.func(
            query=f"普通问题-{id(store)}",
            state={"retrieved_docs": []},
            tool_call_id="call_t2",
        )
        assert (get_metrics().get_metric("ops_flywheel_searches_total",
                                         {"auto_hit": "false"})["value"] == 1)
        assert get_metrics().get_metric("ops_flywheel_hit_docs_total") is None
    finally:
        retrieval_context.set_knowledge_store(None)


# 真实 ToolNode 链路的 citations 回归验证由 scripts/_flywheel_live_check.py 承担
# （真实 LLM + 真实 ToolNode + 真实 qdrant；mock 复现会与 langchain 消息内部机制纠缠）
