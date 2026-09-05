"""
工具定义（facade）

LangGraph Agent 使用的工具按业务域拆分（T9）：
- retrieval_context.py: 检索上下文与查询重写（ContextVar 组 / 用户身份 / 知识库注入）
- tool_cache.py:        工具结果缓存（ToolCache + _tool_cache 单例）
- tools_retrieval.py:   search_knowledge（RAG 混合检索 + 权限过滤 + 质量重试）
- tools_web.py:         web_search / crawl_webpage / generate_content
- tools_memory.py:      get_user_profile / save_memory / search_memory
- tools_ops.py:         运维诊断 mock 工具 + 评测场景开关与 mock 数据

本模块保留 create_tools 工厂与历史导入路径的全量再导出：agent.py、
alert_service、tests、evaluation 既有 `from ...tools import X` 写法与
monkeypatch 落点（如 agent.pop_retrieval_buffer）依赖此 facade，请勿改为
直接从子模块导入。

例外：运行期重绑定的可变全局（_knowledge_store / _query_rewriter_llm /
_eval_scenario_override）不在此再导出——import 快照会失效，跨模块读取须经
所属子模块的模块属性访问（原 `set_retriever`/`_retriever` 为死代码已删除）。
"""

from .retrieval_context import (
    _REWRITE_TRIGGERS,
    _append_retrieval_buffer,
    _conversation_context,
    _current_org_id,
    _current_user_id,
    _extract_conv_context_from_messages,
    _get_buffer,
    _needs_query_rewrite,
    _query_rewrite_cache,
    _query_rewrite_cache_lock,
    _retrieval_buffer,
    _rewrite_query_with_context,
    pop_retrieval_buffer,
    set_conversation_context,
    set_current_org_id,
    set_current_user_id,
    set_knowledge_store,
    set_query_rewriter_llm,
)
from .tool_cache import ToolCache, _tool_cache
from .tools_memory import get_user_profile, save_memory, search_memory
from .tools_ops import (
    _EVAL_SCENARIO_MOCKS,
    _eval_scenario,
    analyze_chart,
    create_incident_ticket,
    get_recent_changes,
    get_service_dependencies,
    query_logs,
    query_metrics,
    set_eval_scenario,
)
from .tools_retrieval import (
    _LOW_SCORE_THRESHOLD,
    _MIN_RESULT_COUNT,
    _evaluate_retrieval_quality,
    _merge_search_results,
    search_knowledge,
)
from .tools_web import crawl_webpage, generate_content, web_search

__all__ = [
    # --- 检索上下文 / 查询重写 ---
    "_REWRITE_TRIGGERS",
    "_append_retrieval_buffer",
    "_conversation_context",
    "_current_org_id",
    "_current_user_id",
    "_extract_conv_context_from_messages",
    "_get_buffer",
    "_needs_query_rewrite",
    "_query_rewrite_cache",
    "_query_rewrite_cache_lock",
    "_retrieval_buffer",
    "_rewrite_query_with_context",
    "pop_retrieval_buffer",
    "set_conversation_context",
    "set_current_org_id",
    "set_current_user_id",
    "set_knowledge_store",
    "set_query_rewriter_llm",
    # --- 缓存 ---
    "ToolCache",
    "_tool_cache",
    # --- 检索质量 ---
    "_LOW_SCORE_THRESHOLD",
    "_MIN_RESULT_COUNT",
    "_evaluate_retrieval_quality",
    "_merge_search_results",
    # --- 工具（13） ---
    "search_knowledge",
    "web_search",
    "crawl_webpage",
    "generate_content",
    "get_user_profile",
    "save_memory",
    "search_memory",
    "query_metrics",
    "query_logs",
    "analyze_chart",
    "get_recent_changes",
    "create_incident_ticket",
    "get_service_dependencies",
    # --- 评测场景 ---
    "_EVAL_SCENARIO_MOCKS",
    "_eval_scenario",
    "set_eval_scenario",
    # --- 工厂 ---
    "create_tools",
]

# 运行期重绑定的可变全局不在模块顶层再导出（快照失效风险），
# 经 PEP 562 __getattr__ 提供活引用，兼容极端情况下的属性访问。
_RUNTIME_REBIND_GLOBALS = ("_knowledge_store", "_query_rewriter_llm", "_eval_scenario_override")


def __getattr__(name: str):
    if name in _RUNTIME_REBIND_GLOBALS:
        import importlib

        if name == "_eval_scenario_override":
            mod = importlib.import_module(".tools_ops", __package__)
        else:
            mod = importlib.import_module(".retrieval_context", __package__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def create_tools() -> list:
    """创建工具列表"""
    return [
        search_knowledge,
        query_metrics,
        query_logs,
        analyze_chart,
        get_recent_changes,
        get_service_dependencies,
        create_incident_ticket,
        web_search,
        crawl_webpage,
        generate_content,
        get_user_profile,
        save_memory,
        search_memory,
    ]
