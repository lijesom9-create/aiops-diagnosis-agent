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

        Returns:
            str: 组装好的上下文
        """
        parts = []

        # 1. 核心记忆（Agent 人设 + 用户画像）
        if include_core:
            core_context = self.core_memory.get_context(user_id)
            if core_context:
                parts.append(core_context)

        # 2. 回忆记忆（对话历史）
        if include_recall:
            history_context = self.recall_memory.get_context_string(
                user_id, session_id, max_history_turns
            )
            if history_context:
                parts.append(history_context)

        # 3. 档案记忆（用户笔记、学习记录）
        if include_archival:
            archival_context = self.archival_memory.get_context_string(
                user_id, query, max_archival_results
            )
            if archival_context:
                parts.append(archival_context)

        # 4. RAG 知识（文档知识库）
        if include_rag and self.rag_retriever:
            knowledge_results = self.search_knowledge(query, max_rag_results)
            if knowledge_results:
                parts.append("## 知识库")
                for i, result in enumerate(knowledge_results, 1):
                    title = result.get("metadata", {}).get("title", "")
                    content = result.get("content", "")
                    if rag_content_limit is not None and rag_content_limit > 0:
                        content = content[:rag_content_limit]
                    parts.append(f"[{i}] {title}\n{content}")

        return "\n\n".join(parts)

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
