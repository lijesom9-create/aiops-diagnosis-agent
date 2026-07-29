"""
记忆管理器 (Memory Manager)

整合核心记忆、回忆记忆、档案记忆和 RAG 知识，
提供统一的记忆管理接口。

架构：
┌─────────────────────────────────────────────────────────┐
│                    MemoryManager                        │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ CoreMemory  │  │ RecallMemory│  │ArchivalMemory│     │
│  │ (核心记忆)  │  │ (回忆记忆)  │  │ (档案记忆)  │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
│         │                │                │             │
│         └────────────────┼────────────────┘             │
│                          ↓                              │
│                   ┌─────────────┐                       │
│                   │ RAG Retriever│                       │
│                   │ (知识检索)   │                       │
│                   └─────────────┘                       │
└─────────────────────────────────────────────────────────┘
"""

from typing import Dict, Any, Optional, List
from loguru import logger

from .core_memory import CoreMemory, UserProfile, AgentPersona
from .recall_memory import RecallMemory, ConversationTurn
from .archival_memory import ArchivalMemory, MemoryEntry


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

        # 初始化记忆模块
        self.core_memory = CoreMemory()
        self.recall_memory = RecallMemory(embedding_model=embedding_model)
        self.archival_memory = ArchivalMemory(
            embedding_model=embedding_model,
            vector_store=vector_store,
        )

        logger.info("MemoryManager 初始化完成")

    # ========== 核心记忆操作 ==========

    def get_user_profile(self, user_id: str) -> UserProfile:
        """获取用户画像"""
        return self.core_memory.get_user_profile(user_id)

    def update_user_profile(self, user_id: str, **kwargs) -> UserProfile:
        """更新用户画像"""
        return self.core_memory.update_user_profile(user_id, **kwargs)

    def add_weak_topic(self, user_id: str, topic: str):
        """添加薄弱知识点"""
        self.core_memory.add_weak_topic(user_id, topic)

    def add_strong_topic(self, user_id: str, topic: str):
        """添加擅长领域"""
        self.core_memory.add_strong_topic(user_id, topic)

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
    ) -> List[Dict]:
        """搜索知识库"""
        if not self.rag_retriever:
            return []

        results = self.rag_retriever.search(query, top_k=top_k)
        return [r.to_dict() for r in results]

    # ========== 上下文组装 ==========

    def build_context(
        self,
        query: str,
        user_id: str,
        session_id: Optional[str] = None,
        include_core: bool = True,
        include_recall: bool = True,
        include_archival: bool = True,
        include_rag: bool = True,
        max_history_turns: int = 10,
        max_rag_results: int = 5,
        max_archival_results: int = 3,
        rag_content_limit: Optional[int] = 200,
        max_context_tokens: Optional[int] = None,
    ) -> str:
        """
        组装完整的上下文

        Args:
            query: 用户查询
            user_id: 用户 ID
            session_id: 会话 ID
            include_core: 是否包含核心记忆
            include_recall: 是否包含回忆记忆
            include_archival: 是否包含档案记忆
            include_rag: 是否包含 RAG 知识
            max_history_turns: 最大历史轮次
            max_rag_results: 最大 RAG 结果数
            max_archival_results: 最大档案结果数
            rag_content_limit: RAG 知识单条内容截断长度，None 或 -1 表示不截断
            max_context_tokens: P1-2 上下文 token 预算（None 时从 settings 读取，-1 表示不限制）
                超预算时按优先级（RAG > archival > recall > core）截断或丢弃

        Returns:
            str: 组装好的上下文
        """
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

        # 1. 核心记忆（Agent 人设 + 用户画像）—— 最高优先级，不可截断
        if include_core:
            core_context = self.core_memory.get_context(user_id)
            if core_context:
                parts_with_meta.append({
                    "name": "core",
                    "content": core_context,
                    "priority": 100,         # 最高，必保留
                    "truncatable": False,
                })

        # 2. 回忆记忆（对话历史）—— 低优先级，最早可被丢弃
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
            knowledge_results = self.search_knowledge(query, max_rag_results)
            if knowledge_results:
                # 把每条 RAG 结果拆成独立 part（按 score 从高到低排序已由检索保证）
                for i, result in enumerate(knowledge_results, 1):
                    title = result.get("metadata", {}).get("title", "")
                    content = result.get("content", "")
                    if rag_content_limit is not None and rag_content_limit > 0:
                        content = content[:rag_content_limit]
                    parts_with_meta.append({
                        "name": f"rag_{i}",
                        "content": f"[{i}] {title}\n{content}",
                        "priority": 60 - i,  # 越靠前优先级越高
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

    def build_prompt(
        self,
        query: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> str:
        """
        构建完整的 Prompt

        Args:
            query: 用户查询
            user_id: 用户 ID
            session_id: 会话 ID

        Returns:
            str: 完整的 Prompt
        """
        # 组装上下文
        context = self.build_context(
            query=query,
            user_id=user_id,
            session_id=session_id,
        )

        # 构建 Prompt
        prompt = f"""{context}

## 用户问题
{query}

## 要求
1. 基于知识库上下文回答
2. 考虑对话历史，保持连贯
3. 如果是追问，理解上下文
4. 引用来源使用 [1]、[2] 标记
5. 如果上下文没有相关信息，明确说明

## 回答"""

        return prompt

    # ========== 清理操作 ==========

    def clear_user_data(self, user_id: str) -> Dict[str, int]:
        """清空用户的所有数据"""
        stats = {
            "sessions": self.recall_memory.clear_user_history(user_id),
            "entries": self.archival_memory.clear_user_entries(user_id),
            "profile": 1 if self.core_memory.delete_user(user_id) else 0,
        }
        logger.info(f"清空用户数据: {user_id} - {stats}")
        return stats

    def get_stats(self, user_id: str) -> Dict[str, Any]:
        """获取用户记忆统计"""
        profile = self.core_memory.get_user_profile(user_id)
        sessions = self.recall_memory.get_all_sessions(user_id)
        entries = self.archival_memory._user_entries.get(user_id, [])

        return {
            "user_id": user_id,
            "has_profile": bool(profile.name),
            "weak_topics": len(profile.weak_topics),
            "strong_topics": len(profile.strong_topics),
            "sessions": len(sessions),
            "total_turns": sum(len(s.turns) for s in sessions),
            "archival_entries": len(entries),
        }
