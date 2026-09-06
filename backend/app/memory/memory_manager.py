"""
记忆管理器 (Memory Manager)

整合回忆记忆（对话历史）、档案记忆和 RAG 知识，
提供统一的记忆管理接口。

架构：
┌─────────────────────────────────────────────────────────┐
│                    MemoryManager                        │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐                       │
│  │ RecallMemory│  │ArchivalMemory│                      │
│  │ (回忆记忆)  │  │ (档案记忆)  │                       │
│  └─────────────┘  └─────────────┘                       │
│         │                │                │             │
│         └────────────────┼────────────────┘             │
│                          ↓                              │
│                   ┌─────────────┐                       │
│                   │ RAG Retriever│                       │
│                   │ (知识检索)   │                       │
│                   └─────────────┘                       │
└─────────────────────────────────────────────────────────┘
"""

from typing import Any, Dict, List, Optional

from loguru import logger

from .archival_memory import ArchivalMemory, MemoryEntry
from .recall_memory import ConversationTurn, RecallMemory


class MemoryManager:
    """
    记忆管理器

    整合所有记忆层次，提供统一接口。
    """

    def __init__(
        self,
        embedding_model=None,
        vector_store=None,
        rag_retriever=None,
    ):
        """
        Args:
            embedding_model: Embedding 模型
            vector_store: 向量数据库
            rag_retriever: RAG 检索器
        """
        self.embedding_model = embedding_model
        self.vector_store = vector_store
        self.rag_retriever = rag_retriever

        # 初始化记忆模块（画像层已随教学域裁剪移除；保留会话/档案记忆）
        self.recall_memory = RecallMemory(embedding_model=embedding_model)
        self.archival_memory = ArchivalMemory(
            embedding_model=embedding_model,
            vector_store=vector_store,
        )

        logger.info("MemoryManager 初始化完成")

    # ========== 对话操作 ==========

    def add_user_message(
        self,
        user_id: str,
        content: str,
        session_id: Optional[str] = None,
    ) -> ConversationTurn:
        """添加用户消息"""
        return self.recall_memory.add_message(
            user_id=user_id,
            role="user",
            content=content,
            session_id=session_id,
        )

    def add_ai_message(
        self,
        user_id: str,
        content: str,
        session_id: Optional[str] = None,
    ) -> ConversationTurn:
        """添加 AI 消息"""
        return self.recall_memory.add_message(
            user_id=user_id,
            role="assistant",
            content=content,
            session_id=session_id,
        )

    def get_recent_history(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[ConversationTurn]:
        """获取最近对话历史"""
        return self.recall_memory.get_recent_history(user_id, session_id, limit)

    def search_history(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> List[ConversationTurn]:
        """语义搜索对话历史"""
        return self.recall_memory.search_history(query, user_id, top_k)

    # ========== 档案记忆操作 ==========

    def add_memory(
        self,
        user_id: str,
        content: str,
        category: str = "",
        tags: List[str] = None,
        metadata: Dict = None,
        check_conflict: bool = True,
    ) -> MemoryEntry:
        """
        添加档案记忆

        Args:
            check_conflict: 是否检测冲突（默认开启）
        """
        if check_conflict:
            entry, conflict_reason = self.archival_memory.add_with_conflict_check(
                user_id=user_id,
                content=content,
                category=category,
                tags=tags,
                metadata=metadata,
            )
            if conflict_reason:
                logger.info(f"记忆冲突: {conflict_reason}, 已更新旧记忆")
            return entry
        else:
            return self.archival_memory.add(
                user_id=user_id,
                content=content,
                category=category,
                tags=tags,
                metadata=metadata,
            )

    def search_memory(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        category: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """搜索档案记忆"""
        return self.archival_memory.search(
            query=query,
            user_id=user_id,
            top_k=top_k,
            category=category,
        )

    # ========== RAG 检索 ==========

    def search_knowledge(
        self,
        query: str,
        top_k: int = 5,
        chat_history: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """搜索知识库

        Args:
            chat_history: 对话历史（用于多轮对话改写）
        """
        if not self.rag_retriever:
            return []

        results = self.rag_retriever.search(query, top_k=top_k, chat_history=chat_history)
        return [r.to_dict() for r in results]

    # ========== 上下文组装 ==========

    def build_context(
        self,
        query: str,
        user_id: str,
        session_id: Optional[str] = None,
        include_recall: bool = True,
        include_archival: bool = True,
        include_rag: bool = True,
        max_history_turns: int = 10,
        max_rag_results: Optional[int] = None,
        max_archival_results: int = 3,
        rag_content_limit: Optional[int] = 200,
        max_context_tokens: Optional[int] = None,
        chat_history: Optional[List[Dict]] = None,
    ) -> str:
        """
        组装完整的上下文

        Args:
            query: 用户查询
            user_id: 用户 ID
            session_id: 会话 ID
            include_recall: 是否包含回忆记忆
            include_archival: 是否包含档案记忆
            include_rag: 是否包含 RAG 知识
            max_history_turns: 最大历史轮次
            max_rag_results: 最大 RAG 结果数
            max_archival_results: 最大档案结果数
            rag_content_limit: RAG 知识单条内容截断长度，None 或 -1 表示不截断
            max_context_tokens: P1-2 上下文 token 预算（None 时从 settings 读取，-1 表示不限制）
                超预算时按优先级（RAG > archival > recall）截断或丢弃

        Returns:
            str: 组装好的上下文
        """
        # RAG top_k: None 时从 settings 读取
        if max_rag_results is None:
            try:
                from ..core.config import settings
                max_rag_results = getattr(settings, "RAG_TOP_K", 8)
            except Exception:
                max_rag_results = 8

        # P1-2: 解析 token 预算
        if max_context_tokens is None:
            try:
                from ..core.config import settings
                max_context_tokens = getattr(settings, "RAG_MAX_CONTEXT_TOKENS", 6000)
            except Exception:
                max_context_tokens = 6000

        unlimited = (max_context_tokens is None) or (max_context_tokens < 0)

        # 收集 parts（每个 part 是 dict，便于做预算控制）
        parts_with_meta: List[Dict[str, Any]] = []

        # 1. 回忆记忆（对话历史）—— 低优先级，最早可被丢弃
        if include_recall:
            history_context = self.recall_memory.get_context_string(
                user_id, session_id, max_history_turns
            )
            if history_context:
                parts_with_meta.append({
                    "name": "history",
                    "content": history_context,
                    "priority": 20,         # 低优先级，超预算优先丢
                    "truncatable": True,
                })

        # 3. 档案记忆（用户笔记、学习记录）—— 中优先级
        if include_archival:
            archival_context = self.archival_memory.get_context_string(
                user_id, query, max_archival_results
            )
            if archival_context:
                parts_with_meta.append({
                    "name": "archival",
                    "content": archival_context,
                    "priority": 40,
                    "truncatable": True,
                })

        # 4. RAG 知识（文档知识库）—— 高优先级（最新召回价值最高）
        if include_rag and self.rag_retriever:
            knowledge_results = self.search_knowledge(query, max_rag_results, chat_history=chat_history)
            if knowledge_results:
                # 把每条 RAG 结果拆成独立 part（按 score 从高到低排序已由检索保证）
                for i, result in enumerate(knowledge_results, 1):
                    title = result.get("metadata", {}).get("title", "")
                    content = result.get("content", "")
                    if rag_content_limit is not None and rag_content_limit > 0:
                        content = content[:rag_content_limit]

                    # 多模态 RAG：对图片类型的子块做特殊标注
                    element_type = result.get("metadata", {}).get("element_type", "")
                    image_path = result.get("metadata", {}).get("image_path", "")
                    image_type = result.get("metadata", {}).get("image_type", "")

                    if element_type == "image":
                        # 图片块：caption 已在 content 中，补充"图片引用"标记
                        # 让 LLM 知道这是一个图片描述而非纯文本
                        type_label = f"[{image_type}图片]" if image_type else "[图片]"
                        content_str = f"{type_label} {content}"
                        if image_path:
                            # image_path 仅供前端引用，不写进 LLM 上下文避免 token 浪费
                            # 但保留在 part 元数据里供上游使用
                            pass
                        parts_with_meta.append({
                            "name": f"rag_{i}",
                            "content": f"[{i}] {title}\n{content_str}",
                            "priority": 60 - i * 5,
                            "truncatable": True,
                            "image_path": image_path,
                            "image_type": image_type,
                        })
                    else:
                        parts_with_meta.append({
                            "name": f"rag_{i}",
                            "content": f"[{i}] {title}\n{content}",
                            "priority": 60 - i * 5,  # 越靠前优先级越高；i>=5 时低于 archival(40)，避免挤占历史对话预算
                            "truncatable": True,
                        })

        # P1-2: 按 token 预算筛选
        if unlimited:
            selected_parts = parts_with_meta
            stats = {"total_tokens": 0, "truncated": 0, "dropped": 0}
        else:
            try:
                from ..core.token_counter import fit_parts_to_budget
                # 为用户查询和系统提示预留 token
                reserved = max(500, len(query) // 2)
                selected_parts, stats = fit_parts_to_budget(
                    parts_with_meta,
                    max_tokens=max_context_tokens,
                    reserved_for_query=reserved,
                )
                if stats["dropped"] > 0 or stats["truncated"] > 0:
                    logger.info(
                        f"P1-2 上下文预算控制: total={stats['total_tokens']} tokens, "
                        f"truncated={stats['truncated']}, dropped={stats['dropped']}"
                    )
            except Exception as e:
                logger.warning(f"token 预算控制失败，使用全部上下文: {e}")
                selected_parts = parts_with_meta
                stats = {"total_tokens": 0, "truncated": 0, "dropped": 0}

        # 拼接为最终字符串
        contents = [p["content"] for p in selected_parts if p.get("content")]
        return "\n\n".join(contents)

    # ========== 清理操作 ==========
