"""降级路径可观测单元测试（批次 A，P1）——全离线。

验证各降级埋点在触发时产生对应指标快照（get_metrics 单例，测试前 reset）。
"""
import pytest

from app.observability.metrics import get_metrics


@pytest.fixture(autouse=True)
def _clean_metrics():
    get_metrics().reset()
    yield
    get_metrics().reset()


def test_knowledge_store_missing_metric():
    """search_knowledge 在知识库未初始化时计数 + 告警日志"""
    from app.langgraph_agent import retrieval_context
    from app.langgraph_agent.tools_retrieval import search_knowledge

    retrieval_context.set_knowledge_store(None)
    result = search_knowledge.invoke({
        "args": {"query": "测试问题"}, "name": "search_knowledge",
        "type": "tool_call", "id": "t0",
    })
    # 带 InjectedToolCallId 的工具在完整 ToolCall 形式下返回 ToolMessage 包装
    assert (result.content if hasattr(result, "content") else result) == "知识库未初始化"
    m = get_metrics().get_metric("rag_knowledge_store_missing_total")
    assert m is not None and m["value"] >= 1


def test_rewrite_degraded_llm_missing():
    """重写 LLM 未注入时静默降级并计数（reason=llm_missing）"""
    from app.langgraph_agent import retrieval_context as rc

    rc.set_query_rewriter_llm(None)
    token = rc._conversation_context.set(["user: 你好"])
    try:
        result = rc._rewrite_query_with_context("它的路由怎么配置？")
        assert result == "它的路由怎么配置？"  # 降级返回原查询
        m = get_metrics().get_metric("rag_rewrite_degraded_total", {"reason": "llm_missing"})
        assert m is not None and m["value"] >= 1
    finally:
        rc._conversation_context.reset(token)


def test_agent_mcp_status_counter():
    """MCP 终态迁移计数"""
    from app.langgraph_agent.agent import LangGraphAgent

    agent = LangGraphAgent(
        llm_model="mock-model", llm_base_url="http://localhost",
        llm_api_key="dummy", checkpoint_path=None,
    )
    agent._set_mcp_status("timeout")
    m = get_metrics().get_metric("agent_mcp_status_total", {"status": "timeout"})
    assert m is not None and m["value"] >= 1
    assert agent._mcp_status == "timeout"


def test_ai_failover_metric():
    """容灾切换计数（复用 P0 的 FailoverProvider 行为）"""
    from app.core.ai_service import FailoverProvider, NonRetryableLLMError

    class _Stub:
        def __init__(self, fail_first=0):
            self.calls = 0
            self.fail_first = fail_first

        async def chat(self, messages, tools=None):
            self.calls += 1
            if self.calls <= self.fail_first:
                raise NonRetryableLLMError("402 insufficient")
            return {"content": "fallback"}

        async def chat_stream(self, messages):
            yield "x"

    fp = FailoverProvider(_Stub(fail_first=1), _Stub())

    import asyncio
    result = asyncio.run(fp.chat([{"role": "user", "content": "hi"}]))
    assert result["content"] == "fallback"
    hits = [x for x in get_metrics().get_all_metrics() if x["name"] == "ai_provider_failover_total"]
    assert hits and hits[0]["value"] >= 1


def test_cache_fallback_metric_on_init(monkeypatch):
    """Redis 连接失败降级内存缓存时计数（op=init）"""
    import app.core.cache as cache_mod
    from app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:59999/0")
    cache_mod._cache_instance = None  # 重置单例以触发真实连接尝试
    try:
        backend = cache_mod.get_cache(ttl=60)
        assert isinstance(backend, cache_mod.MemoryCache)
        m = get_metrics().get_metric("cache_fallback_total", {"op": "init"})
        assert m is not None and m["value"] >= 1
    finally:
        cache_mod._cache_instance = None  # 还原单例，避免污染其他测试


def test_ratelimit_fallback_metric(monkeypatch):
    """Redis 不可用时限流降级内存并计数（回归守卫：修过一次 ./. 相对导入错误）"""
    from app.core import rate_limiter as rl_mod
    from app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:59999/0")
    limiter = rl_mod.RateLimiter()
    allowed = limiter.check("user-rl-test")
    assert allowed is True  # 降级内存模式放行，且不因埋点导入错误而 500
    assert (get_metrics().get_metric("ratelimit_fallback_total") is not None)
