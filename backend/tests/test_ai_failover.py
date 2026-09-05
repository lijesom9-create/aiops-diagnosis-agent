"""AI 供应商容灾单元测试（全离线，不打真实 API）。

覆盖：
- OpenAICompatibleProvider 错误分类：401/402/403 快速失败（不空转重试）、5xx 照旧重试、
  流式路径状态码校验（原实现 4xx 会静默返回空响应）
- FailoverProvider：不可重试立即切换 / 可重试耗尽切换 / 主健康不切换 /
  流式仅首 chunk 前切换（中途失败不切换防内容重复）
- create_ai_provider 装配：默认无 fallback / 配置后 FailoverProvider / 无 key 走 Mock
- Agent 链路 wiring：llm_fallback 配置后 llm_with_tools 挂 RunnableWithFallbacks
- get_agent 的 base_url 解析（按 provider 前缀，修复原硬编码 deepseek）
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.ai_service import (
    FailoverProvider,
    MockProvider,
    NonRetryableLLMError,
    OpenAICompatibleProvider,
    _build_single_provider,
    create_ai_provider,
)
from app.core.config import settings


class StubProvider:
    """可编程桩 provider：按脚本逐次返回结果或抛异常（不继承 ABC，鸭子类型即可）"""

    def __init__(self, name="stub", chat_script=None, stream_script=None):
        self.name = name
        self.chat_calls = 0
        self.stream_calls = 0
        self.chat_script = list(chat_script or [])
        self.stream_script = list(stream_script or [])

    async def chat(self, messages, tools=None):
        self.chat_calls += 1
        item = self.chat_script.pop(0) if self.chat_script else {"content": "ok"}
        if isinstance(item, Exception):
            raise item
        return item

    async def chat_stream(self, messages):
        self.stream_calls += 1
        item = self.stream_script.pop(0) if self.stream_script else ["ok"]
        if isinstance(item, Exception):
            raise item
        for c in item:
            yield c


def _mock_httpx_client(status_code: int, body: str = '{"error":{"message":"boom"}}'):
    """构造返回固定状态码的 httpx.AsyncClient mock（非流式路径）"""
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    client.post = AsyncMock(return_value=resp)
    client.is_closed = False
    return client


def _mock_stream_client(status_code: int, body: bytes = b'{"error":{"message":"boom"}}'):
    """构造返回固定状态码的 httpx.AsyncClient mock（流式路径，stream() 为异步上下文）"""
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = status_code
    resp.aread = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=cm)
    client.is_closed = False
    return client


# ========== 错误分类 ==========

@pytest.mark.asyncio
async def test_402_fails_fast_without_retry():
    """402 余额类错误：不空转重试，立即抛 NonRetryableLLMError"""
    p = OpenAICompatibleProvider(api_key="k", model="chat", base_url="http://x")
    p._get_client = AsyncMock(return_value=_mock_httpx_client(402))
    with pytest.raises(NonRetryableLLMError):
        await p.chat([{"role": "user", "content": "hi"}])
    assert p._get_client.await_count == 1


@pytest.mark.asyncio
async def test_401_403_also_nonretryable():
    p = OpenAICompatibleProvider(api_key="k", model="chat", base_url="http://x")
    for status in (401, 403):
        p._get_client = AsyncMock(return_value=_mock_httpx_client(status))
        with pytest.raises(NonRetryableLLMError):
            await p.chat([{"role": "user", "content": "hi"}])
        assert p._get_client.await_count == 1


@pytest.mark.asyncio
async def test_500_retries_three_times(monkeypatch):
    """5xx 属可重试：3 次重试耗尽后抛泛型异常"""
    async def _noop_sleep(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    p = OpenAICompatibleProvider(api_key="k", model="chat", base_url="http://x")
    p._get_client = AsyncMock(return_value=_mock_httpx_client(500))
    with pytest.raises(Exception, match="已重试3次"):
        await p.chat([{"role": "user", "content": "hi"}])
    assert p._get_client.await_count == 3


@pytest.mark.asyncio
async def test_stream_402_raises_instead_of_silent_empty():
    """流式路径 4xx 原实现会静默返回空响应，现应抛 NonRetryableLLMError"""
    p = OpenAICompatibleProvider(api_key="k", model="chat", base_url="http://x")
    p._get_client = AsyncMock(return_value=_mock_stream_client(402))
    with pytest.raises(NonRetryableLLMError):
        async for _ in p.chat_stream([{"role": "user", "content": "hi"}]):
            pass


# ========== FailoverProvider ==========

@pytest.mark.asyncio
async def test_failover_on_nonretryable():
    primary = StubProvider(chat_script=[NonRetryableLLMError("402 insufficient")])
    fallback = StubProvider(chat_script=[{"content": "from-fallback"}])
    fp = FailoverProvider(primary, fallback)
    result = await fp.chat([{"role": "user", "content": "hi"}])
    assert result["content"] == "from-fallback"
    assert primary.chat_calls == 1 and fallback.chat_calls == 1


@pytest.mark.asyncio
async def test_failover_on_retry_exhausted():
    primary = StubProvider(chat_script=[Exception("5xx exhausted")])
    fallback = StubProvider(chat_script=[{"content": "fallback-ok"}])
    fp = FailoverProvider(primary, fallback)
    assert (await fp.chat([{"role": "user", "content": "hi"}]))["content"] == "fallback-ok"


@pytest.mark.asyncio
async def test_primary_healthy_fallback_not_called():
    primary = StubProvider(chat_script=[{"content": "primary-ok"}])
    fallback = StubProvider()
    fp = FailoverProvider(primary, fallback)
    assert (await fp.chat([{"role": "user", "content": "hi"}]))["content"] == "primary-ok"
    assert fallback.chat_calls == 0


@pytest.mark.asyncio
async def test_failover_stream_before_first_chunk():
    """流式：首个 chunk 前失败 → 切换备用，完整输出"""
    primary = StubProvider(stream_script=[Exception("connect refused")])
    fallback = StubProvider(stream_script=[["a", "b", "c"]])
    fp = FailoverProvider(primary, fallback)
    out = [c async for c in fp.chat_stream([{"role": "user", "content": "hi"}])]
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_failover_stream_midway_no_switch():
    """流式：已输出部分内容后失败 → 不切换（防重复输出），异常上抛"""
    async def _broken_stream(messages):
        yield "partial-"
        raise Exception("mid-stream boom")

    primary = StubProvider()
    primary.chat_stream = _broken_stream
    fallback = StubProvider()
    fp = FailoverProvider(primary, fallback)
    with pytest.raises(Exception, match="mid-stream"):
        async for _ in fp.chat_stream([{"role": "user", "content": "hi"}]):
            pass
    assert fallback.stream_calls == 0


# ========== 工厂装配 ==========

def test_create_provider_mock_without_key(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", None)
    assert isinstance(create_ai_provider(), MockProvider)


def test_create_provider_no_fallback_by_default(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", "k")
    monkeypatch.setattr(settings, "AI_MODEL", "deepseek/chat")
    monkeypatch.setattr(settings, "AI_FALLBACK_MODEL", None)
    p = create_ai_provider()
    assert not hasattr(p, "fallback") or not isinstance(p, FailoverProvider)


def test_create_provider_with_failover(monkeypatch):
    monkeypatch.setattr(settings, "AI_API_KEY", "k")
    monkeypatch.setattr(settings, "AI_MODEL", "deepseek/chat")
    monkeypatch.setattr(settings, "AI_FALLBACK_MODEL", "zhipu/glm-4-flash")
    monkeypatch.setattr(settings, "AI_FALLBACK_API_KEY", "k2")
    monkeypatch.setattr(settings, "AI_FALLBACK_BASE_URL", None)
    p = create_ai_provider()
    assert isinstance(p, FailoverProvider)
    assert p.primary.model == "chat"
    assert p.fallback.model == "glm-4-flash"
    assert p.fallback.base_url == "https://open.bigmodel.cn/api/paas/v4"


def test_build_single_provider_unknown_returns_none():
    assert _build_single_provider("nonexistent-provider/xxx", "k") is None


# ========== Agent 链路 wiring ==========

@pytest.mark.asyncio
async def test_agent_fallback_wiring():
    from langchain_core.runnables.fallbacks import RunnableWithFallbacks

    from app.langgraph_agent.agent import LangGraphAgent
    from app.langgraph_agent.retrieval_context import set_query_rewriter_llm

    try:
        agent = LangGraphAgent(
            llm_model="mock-model", llm_base_url="http://localhost",
            llm_api_key="dummy", checkpoint_path=None,
            llm_fallback={"model": "fb-model", "api_key": "k2", "base_url": "http://fallback"},
        )
        assert isinstance(agent.llm_with_tools, RunnableWithFallbacks)
        assert len(agent.llm_with_tools.fallbacks) == 1
        # 查询重写 LLM 同样挂了 fallback
        from app.langgraph_agent.retrieval_context import _query_rewriter_llm
        assert isinstance(_query_rewriter_llm, RunnableWithFallbacks)
    finally:
        # 还原全局 rewriter（避免影响其他测试）
        set_query_rewriter_llm(None)


@pytest.mark.asyncio
async def test_agent_without_fallback_plain_runnable():
    from langchain_core.runnables.fallbacks import RunnableWithFallbacks

    from app.langgraph_agent.agent import LangGraphAgent
    from app.langgraph_agent.retrieval_context import set_query_rewriter_llm

    try:
        agent = LangGraphAgent(
            llm_model="mock-model", llm_base_url="http://localhost",
            llm_api_key="dummy", checkpoint_path=None,
        )
        assert not isinstance(agent.llm_with_tools, RunnableWithFallbacks)
    finally:
        set_query_rewriter_llm(None)


# ========== base_url 解析（get_agent 硬编码 bug 修复） ==========

def test_resolve_base_url_by_provider_prefix():
    from app.api.langgraph import _resolve_llm_base_url
    assert _resolve_llm_base_url("zhipu/glm-4-flash", None) == "https://open.bigmodel.cn/api/paas/v4"
    assert _resolve_llm_base_url("deepseek/chat", None) == "https://api.deepseek.com"


def test_resolve_base_url_explicit_wins():
    from app.api.langgraph import _resolve_llm_base_url
    assert _resolve_llm_base_url("deepseek/chat", "http://custom:8000") == "http://custom:8000"


def test_resolve_base_url_unknown_falls_back_deepseek():
    from app.api.langgraph import _resolve_llm_base_url
    assert _resolve_llm_base_url("nonexistent/xxx", None) == "https://api.deepseek.com"
