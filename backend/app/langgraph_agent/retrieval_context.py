"""检索上下文与查询重写（T9 从 tools.py 拆出）。

集中承载工具链的跨模块可变状态与 per-request 上下文：
- 引用溯源 buffer（ContextVar，per-session 隔离）
- 对话上下文 / 用户身份（ContextVar，供 search_knowledge 做指代消解与权限过滤）
- 查询重写全套（LLM 注入、缓存、指代词规则、上下文重写、替代查询生成）
- 知识库实例注入（set_knowledge_store）

注意：本模块的全局 `_knowledge_store` / `_query_rewriter_llm` 是运行期重绑定的
可变状态，跨模块读取必须走 `retrieval_context._xxx` 模块属性访问（活引用），
不能用 `from ... import _xxx`（import 时快照会永久失效）。
"""

import contextvars
import hashlib
import threading
from typing import Dict, List, Optional

from loguru import logger

# 知识库实例（由 Agent 初始化时经 set_knowledge_store 注入）
_knowledge_store = None


def set_knowledge_store(store):
    """设置知识库"""
    global _knowledge_store
    _knowledge_store = store


# ========== 引用溯源 buffer（per-session 隔离） ==========
# search_knowledge 工具执行时写入结构化检索结果，
# Agent 的 _call_agent 在工具执行后读取并清空。
#
# 原因：LangGraph 1.1.x 的 ToolNode 调用 tool.invoke()，
# 而 .invoke() 对 response_format="content_and_artifact" 只返回 content 字符串，
# artifact 丢失。因此用 buffer 作为可靠传递机制。
#
# 并发安全：使用 contextvars.ContextVar 实现 per-request 隔离，
# 避免多 session 并发时检索结果跨会话泄漏（旧实现用模块级 list + Lock，
# 会把 A 用户的私有文档混入 B 用户的引用列表）。
_retrieval_buffer: contextvars.ContextVar[List[Dict]] = contextvars.ContextVar(
    "retrieval_buffer", default=None
)


def _get_buffer() -> List[Dict]:
    """获取当前 context 的 buffer（未设置时返回空列表）"""
    return _retrieval_buffer.get() or []


def pop_retrieval_buffer() -> List[Dict]:
    """读取并清空检索结果缓冲区（供 Agent._call_agent 调用）"""
    result = list(_get_buffer())
    _retrieval_buffer.set([])
    return result


def _append_retrieval_buffer(docs: List[Dict]) -> None:
    """向缓冲区追加检索结果（供 search_knowledge 工具调用）"""
    current = _get_buffer()
    _retrieval_buffer.set(current + docs)


# ========== 对话上下文管理（多轮对话指代消解，per-session 隔离） ==========
# 存储最近几轮对话文本，供 search_knowledge 做查询重写。
#
# 工作流：
# 1. Agent._call_agent 在调用 LLM 前，调用 set_conversation_context(messages)
# 2. search_knowledge 工具执行时，读取对话上下文做指代消解
# 3. 如果用户查询含指代词（如"它的路由"），用 LLM 根据对话历史重写为完整查询
#
# 设计理由：
# - LangGraph 的 ToolNode 调用 tool.invoke() 时不传 state，工具拿不到对话历史
# - 用 ContextVar 传递，保证并发请求间对话历史不串用

_conversation_context: contextvars.ContextVar[List[str]] = contextvars.ContextVar(
    "conversation_context", default=None
)
# 查询重写用的 LLM（由 Agent 初始化时注入，全局共享）
_query_rewriter_llm = None
# 查询重写缓存（避免相同 query+context 重复调用 LLM，全局共享）
_query_rewrite_cache: Dict[str, str] = {}
_query_rewrite_cache_lock = threading.Lock()

# ========== 当前用户身份（权限隔离，per-session 隔离） ==========
# 与 _conversation_context 同理：ToolNode 调用 tool.invoke() 时不传 state，
# 用 ContextVar 传递 user_id，供 search_knowledge 做文档权限过滤。
# _call_agent 在调用 LLM 前调用 set_current_user_id(user_id)，
# search_knowledge 执行时读取该值传给 hybrid_search_parent_child。
_current_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_user_id", default=None
)


def set_current_user_id(user_id: Optional[str]) -> None:
    """设置当前用户 ID（供 search_knowledge 做文档权限过滤）

    由 Agent._call_agent 在每次调用 LLM 前设置。
    传入 None 表示不限制（如系统级调用）。
    """
    _current_user_id.set(user_id)
    logger.debug(f"set_current_user_id: {user_id}")


_current_org_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_org_id", default=None
)


def set_current_org_id(org_id: Optional[str]) -> None:
    """设置当前组织 ID（与 user_id 一起构成检索可见性过滤）

    来源：task_context["org_id"]（登录用户的 JWT org / 自动诊断的
    DIAGNOSIS_ORG_ID 服务身份）。为空时不启用组织过滤（仅用户级隔离）。
    """
    _current_org_id.set(org_id or None)
    logger.debug(f"set_current_org_id: {org_id}")


def set_conversation_context(messages) -> None:
    """设置对话上下文（供 search_knowledge 做指代消解）

    从 LangGraph state["messages"] 中提取最近 6 条消息的文本，
    存储为 ["user: xxx", "assistant: yyy", ...] 格式。

    Args:
        messages: LangGraph state["messages"] 列表
    """
    ctx: List[str] = []
    # 只取最近 6 条消息（约 3 轮对话），避免上下文过长
    recent = list(messages[-6:]) if messages else []
    for msg in recent:
        role = "user"
        msg_type = getattr(msg, "type", "")
        if msg_type == "ai" or msg_type == "assistant":
            role = "assistant"
        elif msg_type == "human" or msg_type == "user":
            role = "user"
        content = getattr(msg, "content", "")
        if content and isinstance(content, str):
            # 每条消息最多取 200 字符，控制重写 prompt 大小
            ctx.append(f"{role}: {content[:200]}")
    _conversation_context.set(ctx)


def _extract_conv_context_from_messages(messages) -> List[str]:
    """从 LangGraph messages 提取对话上下文（P3: InjectedState 路径使用）

    与 set_conversation_context 逻辑一致，但不写入 contextvars，
    直接返回上下文列表供调用方使用。
    """
    ctx: List[str] = []
    recent = list(messages[-6:]) if messages else []
    for msg in recent:
        role = "user"
        msg_type = getattr(msg, "type", "")
        if msg_type == "ai" or msg_type == "assistant":
            role = "assistant"
        elif msg_type == "human" or msg_type == "user":
            role = "user"
        content = getattr(msg, "content", "")
        if content and isinstance(content, str):
            ctx.append(f"{role}: {content[:200]}")
    return ctx


def set_query_rewriter_llm(llm) -> None:
    """设置查询重写用的 LLM（由 Agent.__init__ 注入）"""
    global _query_rewriter_llm
    _query_rewriter_llm = llm


# 触发查询重写的指代词/省略语（出现这些词时才考虑重写）
_REWRITE_TRIGGERS = {
    "它", "它们", "这个", "那个", "这些", "那些", "其", "该", "此",
    "上面", "前面", "刚才", "上述", "前述",
    "怎么用", "怎么实现", "是什么", "原理是什么", "有啥用",
    "继续", "再说说", "详细说说", "展开说说",
}


def _needs_query_rewrite(query: str) -> bool:
    """规则判断：查询是否需要重写（是否含指代词/省略语）

    Args:
        query: 用户查询

    Returns:
        True 表示需要考虑重写
    """
    if not query:
        return False
    # 查询过短（<5 字符）且不含明确实体时，可能是追问
    if len(query) < 5:
        return True
    # 含指代词/省略语
    for trigger in _REWRITE_TRIGGERS:
        if trigger in query:
            return True
    return False


def _rewrite_query_with_context(query: str, conv_ctx: Optional[List[str]] = None) -> str:
    """根据对话上下文重写查询（消解指代词）

    三级判断（避免不必要的 LLM 调用）：
    1. 无对话上下文 → 跳过（首轮对话或上下文未设置）
    2. 规则判断：查询不含指代词/省略语 → 跳过（完整查询不需要重写）
    3. LLM 重写：有指代词 + 有上下文 → 用 LLM 根据对话历史重写

    Args:
        query: 原始查询
        conv_ctx: 对话上下文列表（P3: 由调用方传入，不再从全局变量读取）
                  为 None 时回退到 contextvars（兼容旧调用方式）

    Returns:
        重写后的查询（重写失败时返回原查询，静默降级）
    """
    # 第一级：无对话上下文，直接返回
    if conv_ctx is None:
        conv_ctx = _conversation_context.get() or []
    if not conv_ctx:
        return query

    # 第二级：规则判断是否需要重写
    if not _needs_query_rewrite(query):
        return query

    # 第三级：用 LLM 重写
    # 先查缓存（相同 query + context 不重复调用）
    context_hash = hashlib.md5(
        "|".join(conv_ctx).encode()
    ).hexdigest()[:8]
    cache_key = f"{query}::{context_hash}"
    with _query_rewrite_cache_lock:
        if cache_key in _query_rewrite_cache:
            cached = _query_rewrite_cache[cache_key]
            logger.debug(f"查询重写缓存命中: '{query}' → '{cached}'")
            return cached

    # LLM 重写
    if not _query_rewriter_llm:
        from ..observability.metrics import safe_increment
        safe_increment("rag_rewrite_degraded_total", 1, labels={"reason": "llm_missing"})
        logger.debug("查询重写 LLM 未初始化，跳过")
        return query

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        # 构建重写 prompt（轻量，限制输出长度）
        context_str = "\n".join(conv_ctx[-4:])  # 最近 2 轮
        rewrite_prompt = f"""根据对话历史，将用户的追问重写为完整的独立查询。

要求：
1. 把指代词（它、这个、那个等）替换为对话中的具体实体
2. 补全省略的主语或宾语
3. 保持原意，不要添加额外信息
4. 直接输出重写后的查询，不要加任何解释或引号

对话历史：
{context_str}

用户追问：{query}

重写后的查询："""

        messages = [
            SystemMessage(content="你是一个查询重写助手，只输出重写后的查询文本。"),
            HumanMessage(content=rewrite_prompt),
        ]

        # 同步调用 LLM（search_knowledge 是同步工具）
        response = _query_rewriter_llm.invoke(messages)
        rewritten = (response.content or "").strip().strip('"\'').strip()

        # 重写结果为空或与原查询相同，不使用
        if not rewritten or rewritten == query:
            logger.debug(f"查询重写无变化: '{query}'")
            return query

        # 限制重写结果长度（避免 LLM 生成过长文本）
        if len(rewritten) > 200:
            rewritten = rewritten[:200]

        # 缓存
        with _query_rewrite_cache_lock:
            _query_rewrite_cache[cache_key] = rewritten
            # 缓存淘汰：超过 100 条时清空一半
            if len(_query_rewrite_cache) > 100:
                _query_rewrite_cache.clear()
                _query_rewrite_cache[cache_key] = rewritten

        logger.info(f"查询重写: '{query}' → '{rewritten}'")
        return rewritten

    except Exception as e:
        from ..observability.metrics import safe_increment
        safe_increment("rag_rewrite_degraded_total", 1, labels={"reason": "llm_error"})
        logger.debug(f"查询重写失败（静默降级）: {e}")
        return query


def _generate_alternative_query(query: str) -> Optional[str]:
    """用 LLM 生成不同角度的替代查询（同义词扩展/概念泛化）

    只在 _query_rewriter_llm 可用时调用。失败时返回 None，静默降级。

    Args:
        query: 原始查询

    Returns:
        替代查询，或 None（LLM 不可用/生成失败）
    """
    if not _query_rewriter_llm:
        from ..observability.metrics import safe_increment
        safe_increment("rag_rewrite_degraded_total", 1, labels={"reason": "llm_missing"})
        return None
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        messages = [
            SystemMessage(content=(
                "将用户的查询改写为不同角度的搜索关键词，用于知识库二次检索。"
                "要求：\n"
                "1. 用同义词或更专业的术语替换原词（如'备份'→'mysqldump/物理备份/逻辑备份'）\n"
                "2. 如果原查询是口语化表述，改写为文档中可能出现的正式表述\n"
                "3. 只输出一个改写后的查询，不要解释，不要引号"
            )),
            HumanMessage(content=query),
        ]
        resp = _query_rewriter_llm.invoke(messages)
        alt = (resp.content or "").strip().strip('"\'').strip()
        # 改写结果不能和原查询完全相同
        if alt and alt != query:
            return alt
    except Exception as e:
        from ..observability.metrics import safe_increment
        safe_increment("rag_rewrite_degraded_total", 1, labels={"reason": "alt_error"})
        logger.debug(f"生成替代查询失败（静默降级）: {e}")
    return None
