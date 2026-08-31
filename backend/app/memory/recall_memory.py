"""
回忆记忆 (Recall Memory)

存储对话历史，支持：
- 最近对话获取
- 语义搜索历史对话
- 对话摘要

特点：
- 记住"发生了什么、什么时候"
- 支持语义搜索
- 可以生成摘要
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class ConversationTurn:
    """对话轮次"""
    id: str
    user_id: str
    role: str  # user, assistant
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def to_context_string(self) -> str:
        """转换为上下文字符串"""
        role_name = "用户" if self.role == "user" else "AI"
        return f"{role_name}: {self.content}"


@dataclass
class ConversationSession:
    """对话会话"""
    session_id: str
    user_id: str
    turns: List[ConversationTurn] = field(default_factory=list)
    summary: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def add_turn(self, role: str, content: str, metadata: Dict = None) -> ConversationTurn:
        """添加对话轮次"""
        turn_id = f"{self.session_id}_{len(self.turns)}"
        turn = ConversationTurn(
            id=turn_id,
            user_id=self.user_id,
            role=role,
            content=content,
            metadata=metadata or {},
        )
        self.turns.append(turn)
        self.updated_at = datetime.now().isoformat()
        return turn

    def get_recent_turns(self, limit: int = 10) -> List[ConversationTurn]:
        """获取最近的对话轮次"""
        return self.turns[-limit:]

    def get_context_string(self, limit: int = 10) -> str:
        """获取对话上下文字符串"""
        recent = self.get_recent_turns(limit)
        return "\n".join(turn.to_context_string() for turn in recent)


class RecallMemory:
    """
    回忆记忆管理器

    管理对话历史，支持：
    - 保存对话
    - 获取最近对话
    - 语义搜索历史对话
    - 生成对话摘要
    """

    def __init__(self, embedding_model=None, max_turns_per_session: int = 100):
        self.embedding_model = embedding_model
        self.max_turns_per_session = max_turns_per_session
        self._sessions: Dict[str, ConversationSession] = {}
        self._user_sessions: Dict[str, List[str]] = {}  # user_id -> [session_ids]
        self._turn_embeddings: Dict[str, List[float]] = {}  # turn_id -> embedding

    def get_or_create_session(self, user_id: str, session_id: Optional[str] = None) -> ConversationSession:
        """获取或创建会话"""
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]

        # 如果没有指定 session_id，获取用户的最新会话
        if not session_id:
            user_sessions = self._user_sessions.get(user_id, [])
            if user_sessions:
                latest_session_id = user_sessions[-1]
                if latest_session_id in self._sessions:
                    return self._sessions[latest_session_id]

        # 创建新会话
        if not session_id:
            session_id = f"session_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        session = ConversationSession(
            session_id=session_id,
            user_id=user_id,
        )
        self._sessions[session_id] = session

        # 更新用户会话列表
        if user_id not in self._user_sessions:
            self._user_sessions[user_id] = []
        self._user_sessions[user_id].append(session_id)

        logger.info(f"创建新会话: {session_id} (用户: {user_id})")
        return session

    def add_message(
        self,
        user_id: str,
        role: str,
        content: str,
        session_id: Optional[str] = None,
        metadata: Dict = None,
    ) -> ConversationTurn:
        """
        添加消息

        Args:
            user_id: 用户 ID
            role: 角色 (user/assistant)
            content: 消息内容
            session_id: 会话 ID（可选）
            metadata: 元数据

        Returns:
            ConversationTurn: 对话轮次
        """
        session = self.get_or_create_session(user_id, session_id)

        # 滑动窗口裁剪：超过上限时删除最早的轮次
        if len(session.turns) >= self.max_turns_per_session:
            removed_turns = session.turns[:len(session.turns) - self.max_turns_per_session + 1]
            for removed in removed_turns:
                self._turn_embeddings.pop(removed.id, None)
            session.turns = session.turns[len(removed_turns):]
            logger.info(f"会话 {session.session_id} 裁剪 {len(removed_turns)} 轮旧对话")

        turn = session.add_turn(role, content, metadata)

        # 生成 embedding（如果模型可用）
        if self.embedding_model:
            try:
                embedding = self._get_embedding(content)
                self._turn_embeddings[turn.id] = embedding
            except Exception as e:
                logger.warning(f"生成 embedding 失败: {e}")

        logger.debug(f"添加消息: {user_id} - {role}: {content[:50]}...")
        return turn

    def get_recent_history(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[ConversationTurn]:
        """获取最近的对话历史"""
        if session_id:
            session = self._sessions.get(session_id)
            if session:
                return session.get_recent_turns(limit)

        # 获取用户最新的会话
        user_sessions = self._user_sessions.get(user_id, [])
        if not user_sessions:
            return []

        latest_session_id = user_sessions[-1]
        session = self._sessions.get(latest_session_id)
        if session:
            return session.get_recent_turns(limit)

        return []

    def get_context_string(
        self,
        user_id: str,
        session_id: Optional[str] = None,
        limit: int = 10,
    ) -> str:
        """获取对话上下文字符串"""
        turns = self.get_recent_history(user_id, session_id, limit)
        if not turns:
            return ""

        parts = ["## 对话历史"]
        for turn in turns:
            parts.append(turn.to_context_string())

        return "\n".join(parts)

    def search_history(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> List[ConversationTurn]:
        """
        语义搜索历史对话

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量

        Returns:
            List[ConversationTurn]: 相关的对话轮次
        """
        if not self.embedding_model:
            logger.warning("未配置 embedding 模型，无法进行语义搜索")
            return []

        # 获取用户的所有会话
        user_sessions = self._user_sessions.get(user_id, [])
        if not user_sessions:
            return []

        # 收集所有对话轮次
        all_turns = []
        for session_id in user_sessions:
            session = self._sessions.get(session_id)
            if session:
                all_turns.extend(session.turns)

        if not all_turns:
            return []

        # 计算查询向量
        query_embedding = self._get_embedding(query)

        # 计算相似度
        scored_turns = []
        for turn in all_turns:
            turn_embedding = self._turn_embeddings.get(turn.id)
            if turn_embedding:
                similarity = self._cosine_similarity(query_embedding, turn_embedding)
                scored_turns.append((similarity, turn))

        # 排序取 top_k
        scored_turns.sort(key=lambda x: x[0], reverse=True)
        return [turn for _, turn in scored_turns[:top_k]]

    def get_all_sessions(self, user_id: str) -> List[ConversationSession]:
        """获取用户的所有会话"""
        session_ids = self._user_sessions.get(user_id, [])
        return [self._sessions[sid] for sid in session_ids if sid in self._sessions]

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        if session_id in self._sessions:
            session = self._sessions[session_id]
            user_id = session.user_id

            # 删除会话
            del self._sessions[session_id]

            # 从用户会话列表中移除
            if user_id in self._user_sessions:
                self._user_sessions[user_id] = [
                    sid for sid in self._user_sessions[user_id]
                    if sid != session_id
                ]

            logger.info(f"删除会话: {session_id}")
            return True
        return False

    def clear_user_history(self, user_id: str) -> int:
        """清空用户的所有历史"""
        session_ids = self._user_sessions.get(user_id, [])
        count = 0

        for session_id in session_ids:
            if session_id in self._sessions:
                del self._sessions[session_id]
                count += 1

        self._user_sessions[user_id] = []
        logger.info(f"清空用户历史: {user_id}, 删除 {count} 个会话")
        return count

    def _get_embedding(self, text: str) -> List[float]:
        """获取文本的 embedding"""
        if hasattr(self.embedding_model, 'embed'):
            return self.embedding_model.embed(text)
        elif hasattr(self.embedding_model, 'encode'):
            result = self.embedding_model.encode(text, normalize_embeddings=True)
            return result.tolist() if hasattr(result, 'tolist') else list(result)
        return []

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        if len(vec1) != len(vec2):
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)
