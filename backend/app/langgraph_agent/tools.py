"""
工具定义

定义 LangGraph Agent 使用的工具。
"""

import contextvars
import hashlib
import threading
from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import tool
from loguru import logger

try:
    from langgraph.prebuilt import InjectedState
except ImportError:
    InjectedState = None  # 兼容旧版 langgraph


# 全局变量，用于存储工具依赖
_retriever = None
_knowledge_store = None


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


# ========== 检索质量评估与低质量重试 ==========
#
# 当首次检索结果过少或最高分过低时，用 LLM 生成不同角度的替代查询重试一次。
# 设计理由：
# - 用户提问角度可能和文档表述角度不一致（如"怎么备份" vs "mysqldump 用法"）
# - LLM 能做同义词扩展和概念泛化，弥补关键词/向量检索的盲区
# - 只在质量低时触发（max_score < 0.35 或结果 < 2），避免额外延迟

# 触发重试的质量阈值
_LOW_SCORE_THRESHOLD = 0.35
_MIN_RESULT_COUNT = 2


def _evaluate_retrieval_quality(results: List[Dict]) -> bool:
    """评估检索结果质量是否达标

    Args:
        results: 检索结果列表

    Returns:
        True 表示质量达标，False 表示需要重试
    """
    if not results or len(results) < _MIN_RESULT_COUNT:
        return False
    max_score = max(r.get("score", 0) for r in results)
    return max_score >= _LOW_SCORE_THRESHOLD


def _generate_alternative_query(query: str) -> Optional[str]:
    """用 LLM 生成不同角度的替代查询（同义词扩展/概念泛化）

    只在 _query_rewriter_llm 可用时调用。失败时返回 None，静默降级。

    Args:
        query: 原始查询

    Returns:
        替代查询，或 None（LLM 不可用/生成失败）
    """
    if not _query_rewriter_llm:
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
        logger.debug(f"生成替代查询失败（静默降级）: {e}")
    return None


def _merge_search_results(results1: List[Dict], results2: List[Dict], limit: int) -> List[Dict]:
    """合并两次检索结果，去重并按分数排序

    去重键：doc_id + content 前 100 字符
    """
    seen: set = set()
    merged: List[Dict] = []
    for doc in results1 + results2:
        key = (doc.get("doc_id", ""), doc.get("content", "")[:100])
        if key in seen:
            continue
        seen.add(key)
        merged.append(doc)
    merged.sort(key=lambda x: x.get("score", 0), reverse=True)
    return merged[:limit]


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
        logger.debug(f"查询重写失败（静默降级）: {e}")
        return query


class ToolCache:
    """工具结果缓存

    底层使用可插拔缓存层（app.core.cache）：
    - 配置了 REDIS_URL → RedisCache（多 worker 共享）
    - 未配置或连接失败 → MemoryCache（进程内）
    """

    def __init__(self, ttl: int = 300):  # 默认 5 分钟过期
        from ..core.cache import get_cache
        self._backend = get_cache(ttl)

    def get(self, func_name: str, *args, **kwargs) -> Optional[Any]:
        """获取缓存"""
        result = self._backend.get(func_name, *args, **kwargs)
        if result is not None:
            logger.debug(f"缓存命中: {func_name}")
        return result

    def set(self, func_name: str, result: Any, *args, **kwargs):
        """设置缓存"""
        self._backend.set(func_name, result, *args, **kwargs)
        logger.debug(f"缓存设置: {func_name}")

    def clear(self):
        """清空缓存"""
        self._backend.clear()


# 全局缓存实例
_tool_cache = ToolCache(ttl=300)


def set_retriever(retriever):
    """设置检索器"""
    global _retriever
    _retriever = retriever


def set_knowledge_store(store):
    """设置知识库"""
    global _knowledge_store
    _knowledge_store = store


@tool
def search_knowledge(
    query: str,
    limit: int = 5,
    service: Optional[str] = None,
    doc_type: Optional[str] = None,
    state: Annotated[dict, InjectedState] if InjectedState else dict = None,
) -> str:
    """
    搜索企业知识库

    使用混合检索（关键词 + 语义 + RRF 融合）从知识库中搜索文档内容。
    自动按当前用户身份过滤，只返回该用户有权访问的文档（公共文档 + 本人私有文档）。

    适用于：
    - 技术问题（API用法、配置方法、操作步骤）
    - 运维操作（备份、部署、监控）
    - 流程规范（上线流程、代码规范）
    - 架构设计、故障排查
    - 任何涉及企业内部文档的问题

    运维诊断场景可按 service/doc_type 精准过滤：
    - service: 限定服务名（如 "payment-service"），只检索该服务相关文档
    - doc_type: 限定文档类型："manual"运维手册 / "incident"历史事故 / "sop"处置预案 / "postmortem"事故复盘
    例如诊断 payment-service 故障时，可传 service="payment-service" + doc_type="incident" 查同类历史事故

    Args:
        query: 搜索关键词
        limit: 返回结果数量
        service: 限定服务名（可选），不填则全库检索
        doc_type: 限定文档类型（可选 manual/incident/sop/postmortem），不填则全部类型

    Returns:
        str: 搜索结果（带 [N] 编号，供 LLM 内联引用）
    """
    global _retriever, _knowledge_store, _tool_cache

    # P3: 优先从 LangGraph state 读取 per-session 数据（InjectedState 注入）
    # 兼容降级：state 为 None 时（直接调用工具非 LangGraph 上下文）回退到 contextvars
    if state is not None:
        # 从 state 读取用户身份（权限隔离）
        task_ctx = state.get("task_context") or {}
        user_id = task_ctx.get("user_id")
        org_id = task_ctx.get("org_id")
        # 从 state.messages 提取对话上下文（指代消解）
        conv_ctx = _extract_conv_context_from_messages(state.get("messages", []))
    else:
        # 兼容降级：直接调用工具时从 contextvars 读取
        user_id = _current_user_id.get()
        org_id = _current_org_id.get()
        conv_ctx = _conversation_context.get() or []

    # 多轮对话指代消解：根据对话上下文重写查询
    # 例如：用户追问"它的路由怎么定义？" → 重写为"FastAPI 的路由怎么定义？"
    rewritten_query = _rewrite_query_with_context(query, conv_ctx)
    # 后续检索和缓存都使用重写后的查询
    search_query = rewritten_query if rewritten_query != query else query

    # 运维场景：按 service/doc_type 精准过滤（如"只查 payment-service 的历史事故"）
    metadata_filter: Optional[Dict[str, Any]] = None
    if service or doc_type:
        metadata_filter = {}
        if service:
            metadata_filter["service"] = service
        if doc_type:
            metadata_filter["doc_type"] = doc_type

    # 检查缓存（缓存键含 user_id/org_id + service + doc_type，避免跨用户/跨组织/跨过滤条件泄漏）
    cached = _tool_cache.get("search_knowledge", search_query, limit, user_id or "", org_id or "",
                             service or "", doc_type or "")
    if cached is not None:
        # 缓存命中时，结构化数据也要写入 buffer（供 Agent 生成 citations）
        if isinstance(cached, (tuple, list)):  # list: Redis JSON 反序列化后
            text, artifact = cached
            _append_retrieval_buffer(artifact)
            return text
        return cached

    try:
        if _knowledge_store:
            # 使用混合检索（BM25 + Vector + RRF 融合 + 查询重写）
            # search_query 已经经过对话指代消解，rewrite_query=True 再做 RAG 层三级重写
            # 传入 user_id 做文档权限过滤（公共文档 + 本人私有文档）
            results = _knowledge_store.hybrid_search_parent_child(
                search_query, top_k=limit, rewrite_query=True,
                user_id=user_id, org_id=org_id,
                metadata_filter=metadata_filter,
            )

            # 检索质量评估：结果过少或最高分过低时，用 LLM 生成替代查询重试一次
            # 设计：用户提问角度可能和文档表述不一致，LLM 做同义词扩展/概念泛化弥补
            if not _evaluate_retrieval_quality(results):
                alt_query = _generate_alternative_query(search_query)
                if alt_query:
                    logger.info(f"检索质量低，替代查询重试: '{search_query}' -> '{alt_query}'")
                    alt_results = _knowledge_store.hybrid_search_parent_child(
                        alt_query, top_k=limit, rewrite_query=False,  # 替代 query 已是 LLM 重写的
                        user_id=user_id, org_id=org_id,
                        metadata_filter=metadata_filter,
                    )
                    if alt_results:
                        results = _merge_search_results(results or [], alt_results, limit)

            if results:
                from ..core.prompt_guard import scan_rag_content
                from ..core.sanitizer import sanitize_text
                formatted = []
                artifact = []  # 结构化引用数据，写入 buffer 供 Agent 生成 citations
                for i, r in enumerate(results[:limit], 1):
                    title = r.get("title", "未知")
                    # 放宽截断到 600 字符，保留更多上下文（父块内容）
                    content = r.get("content", "")[:600]
                    # 源头脱敏：防止敏感信息（手机号/身份证/API Key 等）进入 LLM context
                    content = sanitize_text(content)
                    score = r.get("score", 0)
                    meta = r.get("metadata", {})
                    heading_path = meta.get("heading_path_str", "") or " > ".join(meta.get("heading_path", []))

                    # C4 间接注入扫描：RAG 内容复用 prompt_guard 规则引擎
                    # 不拦截（拦截会误伤正常运维文档），medium 以上告警 + 在文本/artifact 标注
                    injection_risk, injection_note = scan_rag_content(content, source=title)

                    # 给 LLM 的文本：带 [N] 编号，引导内联引用；过期文档标注提醒
                    expired = bool(meta.get("_expired"))
                    expired_note = " ⚠️【文档已过 valid_until 有效期，结论仅供参考】" if expired else ""
                    formatted.append(
                        f"[{i}] **{title}** (相关度: {score:.2f}){expired_note}\n"
                        f"章节: {heading_path}\n"
                        f"{content}"
                    )

                    # 结构化数据：写入 buffer 供 Agent 生成 citations
                    artifact.append({
                        "index": i,
                        "doc_id": meta.get("document_id", ""),
                        "title": title,
                        "heading_path": heading_path,
                        "score": round(score, 4),
                        "content": content,
                        "image_path": meta.get("image_path"),
                        "source": "knowledge_base",
                        # 运维元数据：供证据看板按 doc_type/service 分类
                        "doc_type": meta.get("doc_type", ""),
                        "service": meta.get("service", ""),
                        "incident_id": meta.get("incident_id", ""),
                        # 知识时效标记（检索层对过期文档打的 _expired）
                        "metadata": {"_expired": expired},
                        # C4: 间接注入扫描结果（risk_level + note），供前端/报告标注可疑来源
                        "injection_risk": injection_risk,
                        "injection_note": injection_note,
                    })

                text = "\n\n".join(formatted) + "\n\n---\n请在回答中使用 [1]、[2] 等编号引用上述来源。"
                # 写入 buffer（供 Agent._call_agent 读取）
                _append_retrieval_buffer(artifact)
                # 缓存 (text, artifact) 元组（缓存命中时重放 artifact 到 buffer）
                # 缓存键含 user_id，与缓存检查一致，避免跨用户泄漏
                _tool_cache.set("search_knowledge", (text, artifact), search_query, limit, user_id or "", service or "", doc_type or "")
                return text

            return "未找到相关知识"

        return "知识库未初始化"

    except Exception as e:
        logger.error(f"搜索知识库失败: {e}")
        return f"搜索失败: {str(e)}"


@tool
def web_search(query: str, num_results: int = 5) -> str:
    """
    搜索互联网

    从互联网搜索最新信息。适用于：
    - 查找最新资讯
    - 搜索技术文档
    - 获取实际案例

    Args:
        query: 搜索关键词
        num_results: 返回结果数量

    Returns:
        str: 搜索结果
    """
    global _tool_cache

    # 检查缓存
    cached = _tool_cache.get("web_search", query, num_results)
    if cached is not None:
        return cached

    try:
        # 使用同步方式调用
        import asyncio

        from ..services.web_search import WebSearchService

        service = WebSearchService()

        # 如果在异步环境中，直接调用
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在异步环境中，创建新任务
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        service.search(query=query, max_results=num_results)
                    )
                    results = future.result(timeout=30)
            else:
                results = asyncio.run(
                    service.search(query=query, max_results=num_results)
                )
        except Exception:
            results = asyncio.run(
                service.search(query=query, max_results=num_results)
            )

        if results:
            formatted = []
            for i, r in enumerate(results[:num_results], 1):
                title = r.get("title", "未知")
                url = r.get("url", "")
                content = r.get("content", "")[:200]
                formatted.append(f"{i}. **{title}**\n   链接: {url}\n   {content}")
            result = "\n\n".join(formatted)
            _tool_cache.set("web_search", result, query, num_results)
            return result

        return "未找到相关结果"

    except Exception as e:
        logger.error(f"网络搜索失败: {e}")
        return f"搜索失败: {str(e)}"


@tool
def crawl_webpage(url: str, use_js: bool = False, extract_mode: str = "markdown") -> str:
    """
    爬取网页内容

    爬取指定 URL 的网页内容。适用于：
    - 获取文档内容
    - 爬取博客文章
    - 提取网页正文

    Args:
        url: 网页 URL
        use_js: 是否使用 JavaScript 渲染（适用于 React/Vue 等动态页面），默认 False
        extract_mode: 提取模式：
            - markdown: 转为 Markdown 格式（默认，推荐）
            - article: 只提取正文
            - trafilatura: 智能提取（推荐用于复杂网页）
            - text: 纯文本
            - full: 保留完整 HTML

    Returns:
        str: 网页内容
    """
    try:
        import asyncio

        from ..tools.web_crawler import WebCrawlerTool

        crawler = WebCrawlerTool()

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        crawler.execute(url=url, use_js=use_js, extract_mode=extract_mode)
                    )
                    result = future.result(timeout=60)  # JS 渲染需要更多时间
            else:
                result = asyncio.run(
                    crawler.execute(url=url, use_js=use_js, extract_mode=extract_mode)
                )
        except Exception:
            result = asyncio.run(
                crawler.execute(url=url, use_js=use_js, extract_mode=extract_mode)
            )

        if result.success:
            content = result.data.get("content", "")
            title = result.data.get("title", "未知")
            content_length = result.data.get("content_length", 0)
            return f"**{title}** (长度: {content_length} 字符)\n\n{content[:3000]}"

        return f"爬取失败: {result.error}"

    except Exception as e:
        logger.error(f"爬取网页失败: {e}")
        return f"爬取失败: {str(e)}"


@tool
def generate_content(prompt: str, style: str = "technical") -> str:
    """
    生成内容

    使用 LLM 生成内容。适用于：
    - 生成文章
    - 撰写文档
    - 总结内容

    Args:
        prompt: 生成提示
        style: 风格（technical, casual, formal）

    Returns:
        str: 生成的内容
    """
    try:
        import asyncio

        from ..core.ai_service import ai_service

        system_prompt = {
            "technical": "你是一个技术写作专家，擅长撰写技术文档和博客。",
            "casual": "你是一个轻松的写手，擅长写通俗易懂的内容。",
            "formal": "你是一个正式的写手，擅长撰写商务文档。",
        }.get(style, "你是一个专业的写手。")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, ai_service.chat(messages))
                    response = future.result(timeout=60)
            else:
                response = asyncio.run(ai_service.chat(messages))
        except Exception:
            response = asyncio.run(ai_service.chat(messages))

        return response.get("content", "生成失败")

    except Exception as e:
        logger.error(f"生成内容失败: {e}")
        return f"生成失败: {str(e)}"


@tool
def get_user_profile(user_id: str) -> str:
    """
    获取用户画像

    获取用户的学习风格、薄弱知识点、擅长领域等信息。
    适用于：
    - 了解用户背景
    - 个性化回答
    - 推荐学习内容

    Args:
        user_id: 用户 ID

    Returns:
        str: 用户画像信息
    """
    try:
        from ..shared_services import get_memory_manager
        memory = get_memory_manager()

        profile = memory.core_memory.get_user_profile(user_id)
        if profile:
            return f"""用户画像:
- 姓名: {profile.name or '未设置'}
- 学习风格: {profile.learning_style or '未设置'}
- 薄弱知识点: {', '.join(profile.weak_topics) if profile.weak_topics else '无'}
- 擅长领域: {', '.join(profile.strong_topics) if profile.strong_topics else '无'}"""
        return "用户画像未设置"
    except Exception as e:
        return f"获取用户画像失败: {str(e)}"


@tool
def save_memory(user_id: str, content: str, category: str = "note") -> str:
    """
    保存记忆

    将重要信息保存到用户的档案记忆中。适用于：
    - 保存学习笔记
    - 记录重要信息
    - 保存用户偏好

    Args:
        user_id: 用户 ID
        content: 要保存的内容
        category: 类别（note, learning, important）

    Returns:
        str: 保存结果
    """
    try:
        from ..shared_services import get_memory_manager
        memory = get_memory_manager()

        entry = memory.add_memory(
            user_id=user_id,
            content=content,
            category=category,
        )
        return f"已保存记忆: {entry.content[:50]}..."
    except Exception as e:
        return f"保存记忆失败: {str(e)}"


@tool
def search_memory(user_id: str, query: str) -> str:
    """
    搜索记忆

    从用户的档案记忆中搜索相关信息。适用于：
    - 查找之前保存的笔记
    - 回顾学习记录
    - 检索历史信息

    Args:
        user_id: 用户 ID
        query: 搜索关键词

    Returns:
        str: 搜索结果
    """
    try:
        from ..shared_services import get_memory_manager
        memory = get_memory_manager()

        results = memory.search_memory(query=query, user_id=user_id, top_k=3)
        if results:
            formatted = []
            for i, entry in enumerate(results, 1):
                formatted.append(f"{i}. [{entry.category}] {entry.content[:100]}")
            return "\n".join(formatted)
        return "未找到相关记忆"
    except Exception as e:
        return f"搜索记忆失败: {str(e)}"


# ================= 开放场景评测 Mock：按场景差异化监控信号 =================
# 修"测评污染①"：默认 query_metrics/query_logs/get_recent_changes 对所有 service /
# 场景返回同一套"连接池耗尽 + v2.3.1 发版"固定信号，导致 SC-OPEN-* 场景的 Agent 全被
# 锚定在连接池根因，测不出区分性组合推理。agent_eval 逐场景调用 set_eval_scenario(id)，
# 本组工具读取该开关返回该场景专属信号；未设置（日常对话/真实诊断链路）时保持原 mock。
#
# 注意：不用 ContextVar。工具是同步函数，LangGraph 的 ToolNode 会在独立线程里执行，
# ContextVar 跨线程不保证传播。改用评测进程级全局开关——agent_eval 是单进程串行跑
# 场景，且真实诊断链路从不调用 set_eval_scenario()，因此该开关不会污染生产路径。
_eval_scenario_override: Optional[str] = None


def set_eval_scenario(scenario_id: Optional[str]) -> None:
    """设置/清除评测场景开关（仅供 agent_eval 调用）。"""
    global _eval_scenario_override
    _eval_scenario_override = scenario_id


def _eval_scenario() -> Optional[str]:
    return _eval_scenario_override


# 各开放场景的差异化信号（key = 场景 ID）：
#   metrics：现场应呈现的关键指标（value/baseline/unit/status，query_metrics 拼接）
#   logs：    keyword -> 该场景定向日志（Agent 会按需传 keyword 查证）
#   changes：故障前附近的变更事件（get_recent_changes 返回）
_EVAL_SCENARIO_MOCKS: Dict[str, dict] = {
    "SC-OPEN-001": {  # 对账定时任务凌晨抢连接：池高占用但未耗尽、业务高峰前偶发卡顿
        "title": "对账定时任务抢占连接",
        "metrics": {
            "connection_pool_usage": {"value": 0.82, "baseline": 0.4, "unit": "%", "status": "warning"},
            "pending_connections": {"value": 12, "baseline": 2, "unit": "count", "status": "warning"},
            "checked_out_rows": {"value": 9, "baseline": 3, "unit": "count", "status": "warning"},
            "batch_job_active": {"value": 1, "baseline": 0, "unit": "count", "status": "info"},
            "error_rate": {"value": 0.003, "baseline": 0.005, "unit": "%", "status": "normal"},
            "p99_latency": {"value": 452, "baseline": 120, "unit": "ms", "status": "warning"},
        },
        "logs": {
            "checkout": [
                "[WARN] mysql - checkout blocked 12px 未达池上限（max=20）",
                "[INFO] payment-service - payout 对账子任务占用 6 个 DB 连接",
            ],
            "batch": [
                "[INFO] payout-reconcile - 凌晨 02:00 对账任务启动，预取 6 个 DB 连接",
                "[WARN] payout-reconcile - UPDATE reconcile_status 持锁行进中",
            ],
            "timeout": ["[INFO] payment-service - 无 SQLTransientConnectionException，连接均按时获取"],
        },
        "changes": [{"change_id": "CHG-2026-0915", "type": "schedule", "service": "payment-service",
                     "time": "2026-09-02T00:00:00Z",
                     "description": "新增每日凌晨定时对账任务（重事务，持锁长）"}],
    },
    "SC-OPEN-002": {  # 支付结果回调消费组反复重平衡
        "title": "支付结果回调消费组反复重平衡",
        "metrics": {
            "consumer_lag": {"value": 48200, "baseline": 800, "unit": "msg", "status": "critical"},
            "rebalance_events": {"value": 7, "baseline": 0, "unit": "count", "status": "warning"},
            "coordinator_rebalance": {"value": 1, "baseline": 0, "unit": "count", "status": "warning"},
            "callback_success_rate": {"value": 0.64, "baseline": 1.0, "unit": "%", "status": "warning"},
            "error_rate": {"value": 0.09, "baseline": 0.01, "unit": "%", "status": "warning"},
        },
        "logs": {
            "rebalance": [
                "[WARN] payment-callback-consumer - ConsumerRebalanceStarted: JoinGroup",
                "[WARN] payment-callback-consumer - Stop consuming during rebalance",
            ],
            "lag": ["[WARN] payment-callback-consumer - lag 48k，积压回调触发后续补偿扫描"],
            "callback": [
                "[WARN] payment-callback-consumer - 回调处理阻塞在长事务，超 max.poll.interval.ms",
                "[INFO] payment-callback-consumer - 长事务后 beginOffset 发生跳跃",
            ],
        },
        "changes": [],
    },
    "SC-OPEN-003": {  # 报表查询把读打到主库（读未走从库）
        "title": "报表查询读打到主库",
        "metrics": {
            "master_cpu": {"value": 0.93, "baseline": 0.35, "unit": "%", "status": "critical"},
            "master_read_io": {"value": 0.9, "baseline": 0.2, "unit": "%", "status": "critical"},
            "replica_cpu": {"value": 0.08, "baseline": 0.2, "unit": "%", "status": "normal"},
            "active_conn_master": {"value": 180, "baseline": 40, "unit": "count", "status": "warning"},
            "p99_latency": {"value": 2100, "baseline": 80, "unit": "ms", "status": "critical"},
        },
        "logs": {
            "slow": [
                "[WARN] mysql master - slow_query: SELECT SUM(amount),COUNT(*) FROM orders WHERE status=pending (耗时 18s) 主库执行",
            ],
            "read_only": [
                "[INFO] mysql - 主库出现大量 SELECT 报表查询（read_only=off）",
                "[WARN] mysql - 报表 SQL 未路由到从库，堆在主库",
            ],
            "route": ["[WARN] payment-service - 本应走从库的 SELECT 报表查询落到主库"],
        },
        "changes": [{"change_id": "CHG-2026-0905", "type": "config_change", "service": "mysql",
                     "time": "2026-08-30T15:00:00Z",
                     "description": "新增报表读写分离路由规则（含 order 汇总查询）"}],
    },
    "SC-OPEN-004": {  # 单热点 Key 击穿回源
        "title": "秒杀商品详情热点 Key 击穿回源",
        "metrics": {
            "cache_hit_ratio": {"value": 0.31, "baseline": 0.95, "unit": "%", "status": "critical"},
            "cache_miss_backend": {"value": 82000, "baseline": 2000, "unit": "req", "status": "critical"},
            "hot_key_qps": {"value": 96000, "baseline": 3000, "unit": "req/s", "status": "critical"},
            "db_qps": {"value": 15000, "baseline": 800, "unit": "req/s", "status": "critical"},
            "p99_latency": {"value": 3400, "baseline": 90, "unit": "ms", "status": "critical"},
        },
        "logs": {
            "miss": ["[WARN] redis - 热点 Key 'product:SKU888' 分支大量 cache miss 直接回源 DB"],
            "hotkey": ["[WARN] redis - Key 'product:SKU888' QPS 9.6w，命中率骤降至 31% (50ms 流失效)"],
            "backend": ["[WARN] mysql - 回源码打到 DB，连接/查询队列入秒杀"],
        },
        "changes": [],
    },
    "SC-OPEN-005": {  # 灰度实例签名密钥与服务端不一致产生 401
        "title": "灰度实例签名密钥漂移产生 401",
        "metrics": {
            "signature_401_rate": {"value": 0.33, "baseline": 0.0, "unit": "%", "status": "critical"},
            "http_401_count": {"value": 41200, "baseline": 1200, "unit": "count", "status": "critical"},
            "gray_instance_errors": {"value": 0.38, "baseline": 0.02, "unit": "%", "status": "warning"},
            "stable_instance_errors": {"value": 0.01, "baseline": 0.02, "unit": "%", "status": "normal"},
            "p99_latency": {"value": 140, "baseline": 80, "unit": "ms", "status": "normal"},
        },
        "logs": {
            "401": [
                "[WARN] payment-service - HTTP 401: signature verify failed, invalid sign version",
                "[WARN] payment-service-gray - 请求命中灰度实例，按 v2.4.0 密钥校签",
            ],
            "signature": [
                "[ERROR] payment-service - 401: 客户端按 v2.3.0 密钥签名，服务端灰度实例按 v2.4.0 校签",
                "[INFO] api-gateway - 灰度标签 zone=gray 命中约 1/3 流量",
            ],
            "auth": ["[WARN] payment-service - 签名校验失败集中在灰度实例（稳定实例正常）"],
        },
        "changes": [{"change_id": "CHG-2026-0920", "type": "deploy", "service": "payment-service",
                     "time": "2026-09-03T22:00:00Z",
                     "description": "灰度发布 v2.4.0：签名密钥升级（灰度路由 zone=gray）"}],
    },
}


@tool
def query_metrics(service: str, metric: str = "all", time_range: str = "1h") -> str:
    """查询服务的监控指标（AIOps 故障诊断首选工具），支持指定时间窗。

    返回服务的关键监控指标，用于故障诊断的"现场取证"。
    拿到指标后应根据异常方向再调 query_logs 定向查日志。
    诊断回顾性故障时务必指定故障发生的时间窗（如告警描述"30 分钟前开始"→ time_range="30m"）。

    Args:
        service: 服务名，如 "payment-service"、"order-service"、"mysql"
        metric: 指标名，默认 "all" 一次拿全。也可指定具体指标名（以服务实际暴露的指标为准）
        time_range: 查询时间窗，默认 "1h"，可选 "5m"/"30m"/"2h"/"6h"/"24h"。
            短窗口（≤2h）返回故障时刻的瞬时值；长窗口（6h/24h）返回窗口均值——
            若长窗口指标正常但短窗口异常，说明故障是近期突发的

    Returns:
        JSON 格式的监控指标数据
    """
    import json

    sid = _eval_scenario()
    if sid and sid in _EVAL_SCENARIO_MOCKS:
        _sm = _EVAL_SCENARIO_MOCKS[sid]
        if metric != "all" and metric in _sm["metrics"]:
            return json.dumps({"service": service, "metric": metric, "time_range": time_range,
                               "scenario": sid, **_sm["metrics"][metric]}, ensure_ascii=False)
        return json.dumps({"service": service, "time_range": time_range, "scenario": sid,
                           "title": _sm["title"], "metrics": _sm["metrics"]}, ensure_ascii=False)

    # 故障时刻的瞬时值（模拟 payment-service 连接池耗尽场景）
    incident_metrics = {
        "error_rate": {"value": 0.38, "baseline": 0.01, "unit": "%", "status": "critical"},
        "connection_pool_usage": {"value": 1.0, "baseline": 0.3, "unit": "%", "status": "critical"},
        "pending_connections": {"value": 87, "baseline": 2, "unit": "count", "status": "critical"},
        "qps": {"value": 4200, "baseline": 1500, "unit": "req/s", "status": "warning"},
        "p99_latency": {"value": 3200, "baseline": 80, "unit": "ms", "status": "critical"},
    }
    # 长窗口均值：故障时段被正常时段稀释，指标回落但仍有残留异常
    averaged_metrics = {
        "error_rate": {"value": 0.06, "baseline": 0.01, "unit": "%", "status": "warning"},
        "connection_pool_usage": {"value": 0.52, "baseline": 0.3, "unit": "%", "status": "warning"},
        "pending_connections": {"value": 9, "baseline": 2, "unit": "count", "status": "warning"},
        "qps": {"value": 1800, "baseline": 1500, "unit": "req/s", "status": "normal"},
        "p99_latency": {"value": 310, "baseline": 80, "unit": "ms", "status": "warning"},
    }

    short_windows = {"5m", "30m", "1h", "2h"}
    metrics = incident_metrics if time_range in short_windows else averaged_metrics

    if metric != "all" and metric in metrics:
        return json.dumps({
            "service": service, "metric": metric, "time_range": time_range,
            **metrics[metric],
        }, ensure_ascii=False)
    return json.dumps({
        "service": service, "time_range": time_range, "metrics": metrics,
    }, ensure_ascii=False)


@tool
def query_logs(service: str, keyword: str, time_range: str = "1h") -> str:
    """查询服务日志，按关键词过滤。

    根据 query_metrics 的异常方向定向查日志找具体异常。
    如资源饱和度高 → keyword="connection" 看连接相关报错。

    Args:
        service: 服务名，如 "payment-service"、"mysql"
        keyword: 日志关键词，如 "connection"、"error"、"slow"、"timeout"
        time_range: 时间范围，默认 "1h"，可选 "5m"/"30m"/"2h"/"24h"

    Returns:
        JSON 格式的日志数据
    """
    import json

    sid = _eval_scenario()
    if sid and sid in _EVAL_SCENARIO_MOCKS:
        _sl = _EVAL_SCENARIO_MOCKS[sid]["logs"]
        matched = _sl.get(keyword)
        if matched is None:
            # 场景下查了不在预设里的关键词：返回场景一致的"无命中"，而非默认连接池日志
            matched = [f"[INFO] {service} - no logs matched keyword '{keyword}'（场景 {sid}）"]
        return json.dumps({"service": service, "keyword": keyword, "time_range": time_range,
                           "scenario": sid, "count": len(matched), "logs": matched},
                          ensure_ascii=False)

    # Mock 日志：根据关键词返回不同的模拟日志
    log_templates = {
        "HikariPool": [
            "[ERROR] 2026-08-02 14:55:23 HikariPool-1 - Connection is not available, timeout 30000ms",
            "[WARN]  2026-08-02 14:55:24 HikariPool-1 - Pool stats: active=10, idle=0, waiting=87",
            "[ERROR] 2026-08-02 14:55:25 HikariPool-1 - Connection pool exhausted (max=10)",
        ],
        "error": [
            "[ERROR] 2026-08-02 14:55:23 payment-service - HTTP 500: upstream connect timed out",
            "[ERROR] 2026-08-02 14:55:26 payment-service - java.sql.SQLTransientConnectionException",
            "[ERROR] 2026-08-02 14:55:28 payment-service - HikariPool-1 - Connection is not available",
        ],
        "slow_query": [
            "[WARN] 2026-08-02 14:54:00 mysql - slow_query detected: SELECT * FROM orders WHERE status='pending' (耗时 12.3s)",
            "[WARN] 2026-08-02 14:55:00 mysql - slow_query detected: UPDATE inventory SET stock=stock-1 (耗时 8.7s)",
        ],
    }

    logs = log_templates.get(keyword, [
        f"[INFO] 2026-08-02 14:55:00 {service} - no logs matched keyword '{keyword}'",
    ])

    return json.dumps({
        "service": service,
        "keyword": keyword,
        "time_range": time_range,
        "count": len(logs),
        "logs": logs,
    }, ensure_ascii=False)


@tool
def analyze_chart(service: str, chart_type: str = "overview") -> str:
    """分析服务监控图表（Grafana 截图），提取图表中的异常模式。

    通过视觉语言模型（VLM）理解监控图表截图，识别曲线异常、跨指标关联，
    输出结构化分析结果。用于故障诊断的"看图取证"，比纯文本指标更直观。

    Args:
        service: 服务名，如 "payment-service"、"order-service"
        chart_type: 图表类型，默认 "overview"。可选："overview"全览 / "connection_pool"连接池 / "latency"延迟

    Returns:
        JSON 格式的图表分析结果（metrics + anomalies + insights）
    """
    import json

    # Mock 数据：模拟 VLM 分析 Grafana 截图后的输出
    # 与 query_metrics 数据一致，但增加 VLM 特有的 anomalies/insights（看图才能发现的形态级信息）
    mock_analysis = {
        "service": service,
        "chart_type": chart_type,
        "source": "grafana_screenshot",
        "metrics": {
            "error_rate": {"value": 0.38, "baseline": 0.01, "unit": "ratio", "status": "critical"},
            "connection_pool_usage": {"value": 1.0, "baseline": 0.3, "unit": "ratio", "status": "critical"},
            "pending_connections": {"value": 87, "baseline": 2, "unit": "count", "status": "critical"},
        },
        "anomalies": [
            {"type": "spike", "description": "error_rate 在 14:30 出现陡升尖峰，从 0.01 飙至 0.38", "severity": "critical"},
            {"type": "saturation", "description": "connection_pool_usage 曲线触顶 100% 并持续横盘，连接池饱和", "severity": "critical"},
            {"type": "correlation", "description": "pending_connections 与 error_rate 同步上升，强相关", "severity": "high"},
        ],
        "insights": "图表显示连接池打满（100%）与错误率飙升（38%）强相关，尖峰始于 14:30，符合连接池耗尽特征",
    }
    return json.dumps(mock_analysis, ensure_ascii=False)


@tool
def get_recent_changes(service: str, hours: int = 24) -> str:
    """查询服务最近 N 小时内的变更事件（发布/配置修改/扩缩容/基础设施操作）。

    变更是生产故障的第一大根因。诊断时必查：若故障时间点附近存在变更，
    应优先沿"变更 → 影响"的因果链定位，而不是只按症状匹配历史经验。

    Args:
        service: 服务名，如 "payment-service"、"order-service"
        hours: 回溯小时数，默认 24。建议与故障时间窗匹配（故障发生在 1 小时内则 hours=1~2）

    Returns:
        JSON 格式的变更事件列表（type: deploy/config_change/scale/infra，change_id，时间，描述）
    """
    import json

    sid = _eval_scenario()
    if sid and sid in _EVAL_SCENARIO_MOCKS:
        _ce = _EVAL_SCENARIO_MOCKS[sid]["changes"]
        return json.dumps({"service": service, "hours": hours, "scenario": sid,
                           "count": len(_ce), "changes": _ce}, ensure_ascii=False)

    # Mock 变更事件：与种子事故对齐——payment-service 事发前 40 分钟有一次发版
    events_by_service = {
        "payment-service": [
            {
                "change_id": "CHG-2026-0812",
                "type": "deploy",
                "service": "payment-service",
                "time": "2026-08-02T14:20:00Z",
                "description": "v2.3.1 发版：新增大额支付风控查询（新增 2 个 DB 查询/笔）",
                "operator": "ci-cd",
            },
            {
                "change_id": "CHG-2026-0805",
                "type": "scale",
                "service": "payment-service",
                "time": "2026-07-30T10:00:00Z",
                "description": "连接池 max-size 保持 10 未调整（上季度容量评估遗留项）",
                "operator": "ops",
            },
        ],
        "order-service": [
            {
                "change_id": "CHG-2026-0809",
                "type": "config_change",
                "service": "order-service",
                "time": "2026-08-01T16:00:00Z",
                "description": "Redis maxmemory 从 4gb 调整为 2gb（成本优化变更）",
                "operator": "ops",
            },
        ],
    }
    events = events_by_service.get(service, [])
    return json.dumps({
        "service": service,
        "hours": hours,
        "count": len(events),
        "changes": events,
    }, ensure_ascii=False)


@tool
def create_incident_ticket(service: str, title: str, root_cause: str,
                           severity: str = "P2", priority: str = "high") -> str:
    """创建故障处理工单，用于诊断结论的落地跟进（诊断 → 行动闭环）。

    使用纪律：
    - 仅在 P1/P2 级故障诊断完成、或用户明确要求创建工单时调用
    - 工单内容应基于已确认的诊断结论，不要在诊断中途调用

    Args:
        service: 受影响的服务名
        title: 工单标题，如 "payment-service 连接池耗尽 - 扩容与慢查询治理"
        root_cause: 诊断出的根因摘要
        severity: 故障级别，P1/P2/P3
        priority: 工单优先级，默认 high

    Returns:
        JSON 格式的创建结果（ticket_id + 状态）
    """
    import json
    import uuid
    from datetime import datetime

    ticket_id = f"TK-{uuid.uuid4().hex[:6].upper()}"
    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({
        "ticket_id": ticket_id,
        "status": "created",
        "service": service,
        "title": title,
        "severity": severity,
        "priority": priority,
        "root_cause": root_cause[:200],
        "created_at": now_str,
        "note": "工单已记录（当前为演示环境，未接入真实工单系统）；请人工跟进处理进度",
    }, ensure_ascii=False)


@tool
def get_service_dependencies(service: str, direction: str = "all") -> str:
    """查询服务的依赖拓扑：下游依赖（本服务调用了谁）与上游调用方（谁调用了本服务）。

    跨服务诊断的关键工具：本服务指标无法解释现象、或怀疑问题出在依赖时，
    用本工具锁定可疑依赖服务，再对该服务补充取证（query_metrics/query_logs 换成该服务名）。

    Args:
        service: 服务名，如 "payment-service"、"mysql"
        direction: 方向，默认 "all"。可选："downstream"只看下游依赖 / "upstream"只看上游调用方 / "all"

    Returns:
        JSON 格式的依赖拓扑（depends_on / called_by 列表）
    """
    import json
    import os

    # 拓扑数据文件化：真实落地时替换 data/service_topology.json
    # （APM 服务地图 / K8s 服务发现自动生成），工具与 prompt 不用改
    topo_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "service_topology.json",
    )
    topology = {}
    try:
        with open(topo_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        topology = {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        # 文件缺失/损坏时的兜底拓扑（与种子事故对齐）
        topology = {
            "payment-service": {
                "depends_on": ["mysql", "redis", "order-service"],
                "called_by": ["api-gateway"],
            },
            "order-service": {
                "depends_on": ["mysql", "inventory-service", "redis"],
                "called_by": ["payment-service", "api-gateway"],
            },
            "mysql": {"depends_on": [], "called_by": ["payment-service", "order-service"]},
            "redis": {"depends_on": [], "called_by": ["payment-service", "order-service"]},
        }

    entry = topology.get(service)
    if not entry:
        return json.dumps({
            "service": service, "direction": direction,
            "depends_on": [], "called_by": [],
            "note": f"拓扑中无 {service} 的记录（可能是基础设施组件或未登记服务），无法跨服务排查",
        }, ensure_ascii=False)

    depends_on = entry.get("depends_on", [])
    called_by = entry.get("called_by", [])
    result = {"service": service, "direction": direction}
    if direction in ("all", "downstream"):
        result["depends_on"] = depends_on
        result["downstream_note"] = "下游依赖故障可能传导到本服务（对可疑依赖补充取证）"
    if direction in ("all", "upstream"):
        result["called_by"] = called_by
        result["upstream_note"] = "本服务故障会向上游调用方传导（影响面评估）"
    return json.dumps(result, ensure_ascii=False)


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
