"""知识库检索工具（T9 从 tools.py 拆出）。

`search_knowledge`：混合检索（关键词 + 语义 + RRF 融合）+ 用户/组织权限过滤 +
多轮对话指代消解 + 检索质量低时替代查询重试 + 源头脱敏与注入扫描。

对 retrieval_context 的可变全局（_knowledge_store）必须经模块属性访问（活引用）；
ContextVar 与函数经名字导入即安全（对象身份稳定）。
"""

from typing import Annotated, Any, Dict, List, Optional

from langchain_core.tools import tool
from loguru import logger

try:
    from langgraph.prebuilt import InjectedState
except ImportError:
    InjectedState = None  # 兼容旧版 langgraph

from . import retrieval_context
from .retrieval_context import (
    _append_retrieval_buffer,
    _conversation_context,
    _current_org_id,
    _current_user_id,
    _extract_conv_context_from_messages,
    _generate_alternative_query,
    _rewrite_query_with_context,
)
from .tool_cache import _tool_cache

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
        knowledge_store = retrieval_context._knowledge_store
        if knowledge_store:
            # 使用混合检索（BM25 + Vector + RRF 融合 + 查询重写）
            # search_query 已经经过对话指代消解，rewrite_query=True 再做 RAG 层三级重写
            # 传入 user_id 做文档权限过滤（公共文档 + 本人私有文档）
            results = knowledge_store.hybrid_search_parent_child(
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
                    alt_results = knowledge_store.hybrid_search_parent_child(
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
