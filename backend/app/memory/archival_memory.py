"""
档案记忆 (Archival Memory)

存储大量信息，支持向量检索：
- 用户笔记
- 学习记录
- 重要信息

特点：
- 容量大
- 支持语义搜索
- 需要显式检索
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str
    user_id: str
    content: str
    category: str = ""  # note, learning, important, etc.
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    superseded_by: str = ""  # 被哪条新记忆替代（空=活跃）
    version: int = 1         # 版本号

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "content": self.content,
            "category": self.category,
            "tags": self.tags,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "superseded_by": self.superseded_by,
            "version": self.version,
        }


class ArchivalMemory:
    """
    档案记忆管理器

    管理大量信息，支持：
    - 保存信息
    - 语义搜索
    - 按类别/标签过滤
    """

    def __init__(self, embedding_model=None, vector_store=None):
        """
        Args:
            embedding_model: Embedding 模型
            vector_store: 向量数据库
        """
        self.embedding_model = embedding_model
        self.vector_store = vector_store
        self._entries: Dict[str, MemoryEntry] = {}
        self._user_entries: Dict[str, List[str]] = {}  # user_id -> [entry_ids]
        self._embeddings: Dict[str, List[float]] = {}  # entry_id -> embedding

    def add(
        self,
        user_id: str,
        content: str,
        category: str = "",
        tags: List[str] = None,
        metadata: Dict = None,
    ) -> MemoryEntry:
        """
        添加记忆条目

        Args:
            user_id: 用户 ID
            content: 内容
            category: 类别
            tags: 标签
            metadata: 元数据

        Returns:
            MemoryEntry: 记忆条目
        """
        entry_id = f"mem_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        entry = MemoryEntry(
            id=entry_id,
            user_id=user_id,
            content=content,
            category=category,
            tags=tags or [],
            metadata=metadata or {},
        )

        self._entries[entry_id] = entry

        # 更新用户条目列表
        if user_id not in self._user_entries:
            self._user_entries[user_id] = []
        self._user_entries[user_id].append(entry_id)

        # 生成 embedding
        if self.embedding_model:
            try:
                embedding = self._get_embedding(content)
                self._embeddings[entry_id] = embedding

                # 如果有向量数据库，也存储
                if self.vector_store:
                    self.vector_store.add(
                        doc_id=entry_id,
                        content=content,
                        metadata={
                            "user_id": user_id,
                            "category": category,
                            "tags": ",".join(tags) if tags else "",
                        },
                    )
            except Exception as e:
                logger.warning(f"生成 embedding 失败: {e}")

        logger.info(f"添加档案记忆: {entry_id} (用户: {user_id}, 类别: {category})")
        return entry

    def add_with_conflict_check(
        self,
        user_id: str,
        content: str,
        category: str = "",
        tags: List[str] = None,
        metadata: Dict = None,
    ) -> Tuple[MemoryEntry, Optional[str]]:
        """
        带冲突检测的记忆添加

        保存前搜索相关旧记忆，检测是否矛盾：
        - 矛盾 → 更新旧记忆（标记为 superseded），添加新记忆并关联
        - 不矛盾 → 正常添加

        Returns:
            (新记忆条目, 冲突描述或 None)
        """
        # 1. 获取该用户所有活跃的记忆（直接从内存查，不走 search 避免无 embedding 时搜不到）
        user_entry_ids = self._user_entries.get(user_id, [])
        active_existing = [
            self._entries[eid] for eid in user_entry_ids
            if eid in self._entries
            and not self._entries[eid].superseded_by
            and self._entries[eid].content.strip()
        ]

        # 按类别过滤（如果指定）
        if category:
            active_existing = [e for e in active_existing if e.category == category]

        if not active_existing:
            entry = self.add(user_id, content, category, tags, metadata)
            return entry, None

        # 2. 检测冲突
        conflict = self._detect_conflict(content, active_existing)

        if conflict:
            old_entry = conflict["old_entry"]
            # 添加新记忆，带版本号
            entry = self.add(user_id, content, category, tags, metadata)
            entry.version = old_entry.version + 1
            # 关联新旧记忆：旧的被新的替代
            old_entry.superseded_by = entry.id
            logger.info(f"记忆冲突检测: 旧={old_entry.id} 被新={entry.id} 替代, 原因={conflict['reason']}")
            return entry, conflict["reason"]

        # 没有冲突，正常添加
        entry = self.add(user_id, content, category, tags, metadata)
        return entry, None

    def _detect_conflict(self, new_content: str, existing_entries: List[MemoryEntry]) -> Optional[Dict]:
        """
        检测新记忆和已有记忆是否矛盾

        使用简单的规则检测，不依赖 LLM（避免额外延迟和成本）。
        检测策略：
        1. 否定词对立：A说"喜欢X"，B说"不喜欢X"
        2. 矛盾模式：A说"是X"，B说"不是X"
        3. 版本更新：A说"大二"，B说"大三"（同一属性的新值）
        """
        new_lower = new_content.lower().strip()

        for entry in existing_entries:
            old_lower = entry.content.lower().strip()

            # 跳过完全相同的内容
            if new_lower == old_lower:
                continue

            # 检测否定对立
            if self._is_negation_pair(new_lower, old_lower):
                return {"old_entry": entry, "reason": "否定对立"}

            # 检测同一属性的新值（如"大二"→"大三"）
            if self._is_attribute_update(new_lower, old_lower):
                return {"old_entry": entry, "reason": "属性更新"}

        return None

    @staticmethod
    def _tokenize_chinese(text: str) -> set:
        """中文分词，返回词集合"""
        try:
            import jieba
            words = set(jieba.cut(text))
            # 过滤停用词和单字符
            stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这", "那", "吗", "呢", "吧", "啊", "更", "用", "以", "为"}
            return {w for w in words if len(w) > 1 and w not in stop_words}
        except ImportError:
            # 回退：按标点和空格分割
            parts = re.split(r'[，。！？、\s]+', text)
            return {w for w in parts if len(w) > 1}

    @classmethod
    def _is_negation_pair(cls, text_a: str, text_b: str) -> bool:
        """检测两条文本是否是否定对立关系"""
        negation_pairs = [
            ("喜欢", "不喜欢"), ("喜欢", "讨厌"), ("喜欢", "不想"),
            ("擅长", "不擅长"), ("擅长", "不熟悉"), ("擅长", "弱"),
            ("了解", "不了解"), ("了解", "不熟悉"),
            ("是", "不是"), ("是", "并非"),
            ("会", "不会"), ("能", "不能"),
            ("需要", "不需要"), ("要", "不要"),
            ("好", "不好"), ("强", "弱"),
            ("多", "少"), ("高", "低"),
        ]

        # 分词
        words_a = cls._tokenize_chinese(text_a)
        words_b = cls._tokenize_chinese(text_b)

        for pos, neg in negation_pairs:
            # 检查是否一方含正面词、另一方含负面词
            has_pos_a = pos in words_a or pos in text_a
            has_neg_b = neg in words_b or neg in text_b
            has_neg_a = neg in words_a or neg in text_a
            has_pos_b = pos in words_b or pos in text_b

            if (has_pos_a and has_neg_b) or (has_neg_a and has_pos_b):
                # 检查是否在讨论同一主题（共享至少一个非否定关键词）
                negation_words = {pos, neg, "不", "没", "非"}
                shared = (words_a & words_b) - negation_words
                if len(shared) >= 1:
                    return True

        return False

    @staticmethod
    def _is_attribute_update(text_a: str, text_b: str) -> bool:
        """检测是否是同一属性的新值更新（如年级、版本等）"""
        # 支持阿拉伯数字和中文数字
        # 例："大二" vs "大三"，"Python 3.8" vs "Python 3.11"，"第3次" vs "第5次"
        cn_digits = '一二三四五六七八九十百千万零'
        pattern = rf'([一-鿿]+)(\d+|[{cn_digits}]+)([一-鿿]*)'

        match_a = re.search(pattern, text_a)
        match_b = re.search(pattern, text_b)

        if match_a and match_b:
            # 前缀相同（如"大"），数字不同（如"二" vs "三"）
            if match_a.group(1) == match_b.group(1) and match_a.group(2) != match_b.group(2):
                return True

        return False

    def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        include_superseded: bool = False,
    ) -> List[MemoryEntry]:
        """
        语义搜索

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量
            category: 类别过滤
            tags: 标签过滤
            include_superseded: 是否包含已失效的记忆

        Returns:
            List[MemoryEntry]: 相关的记忆条目
        """
        # 获取用户的条目
        user_entry_ids = self._user_entries.get(user_id, [])
        if not user_entry_ids:
            return []

        # 过滤
        candidate_ids = user_entry_ids
        if category:
            candidate_ids = [
                eid for eid in candidate_ids
                if self._entries.get(eid) and self._entries[eid].category == category
            ]
        if tags:
            candidate_ids = [
                eid for eid in candidate_ids
                if self._entries.get(eid) and any(t in self._entries[eid].tags for t in tags)
            ]

        # 过滤掉已失效的记忆（除非明确要求包含）
        if not include_superseded:
            candidate_ids = [
                eid for eid in candidate_ids
                if self._entries.get(eid) and not self._entries[eid].superseded_by
            ]

        if not candidate_ids:
            return []

        # 如果有 embedding 模型，使用语义搜索
        if self.embedding_model:
            query_embedding = self._get_embedding(query)

            scored_entries = []
            for entry_id in candidate_ids:
                entry_embedding = self._embeddings.get(entry_id)
                if entry_embedding:
                    similarity = self._cosine_similarity(query_embedding, entry_embedding)
                    scored_entries.append((similarity, self._entries[entry_id]))

            # 排序取 top_k
            scored_entries.sort(key=lambda x: x[0], reverse=True)
            return [entry for _, entry in scored_entries[:top_k]]

        # 否则使用关键词匹配
        query_lower = query.lower()
        scored_entries = []
        for entry_id in candidate_ids:
            entry = self._entries.get(entry_id)
            if entry:
                score = 0
                if query_lower in entry.content.lower():
                    score += 3
                for tag in entry.tags:
                    if query_lower in tag.lower():
                        score += 1
                if score > 0:
                    scored_entries.append((score, entry))

        scored_entries.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored_entries[:top_k]]

    def get_by_id(self, entry_id: str) -> Optional[MemoryEntry]:
        """根据 ID 获取条目"""
        return self._entries.get(entry_id)

    def get_by_category(
        self,
        user_id: str,
        category: str,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """按类别获取条目"""
        user_entry_ids = self._user_entries.get(user_id, [])
        entries = []

        for entry_id in user_entry_ids:
            entry = self._entries.get(entry_id)
            if entry and entry.category == category:
                entries.append(entry)

        return entries[:limit]

    def get_by_tags(
        self,
        user_id: str,
        tags: List[str],
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """按标签获取条目"""
        user_entry_ids = self._user_entries.get(user_id, [])
        entries = []

        for entry_id in user_entry_ids:
            entry = self._entries.get(entry_id)
            if entry and any(t in entry.tags for t in tags):
                entries.append(entry)

        return entries[:limit]

    def update(self, entry_id: str, **kwargs) -> Optional[MemoryEntry]:
        """更新条目"""
        entry = self._entries.get(entry_id)
        if not entry:
            return None

        for key, value in kwargs.items():
            if hasattr(entry, key):
                setattr(entry, key, value)

        entry.updated_at = datetime.now().isoformat()

        # 重新生成 embedding
        if self.embedding_model and "content" in kwargs:
            try:
                embedding = self._get_embedding(entry.content)
                self._embeddings[entry_id] = embedding
            except Exception as e:
                logger.warning(f"更新 embedding 失败: {e}")

        logger.info(f"更新档案记忆: {entry_id}")
        return entry

    def delete(self, entry_id: str) -> bool:
        """删除条目"""
        if entry_id not in self._entries:
            return False

        entry = self._entries[entry_id]
        user_id = entry.user_id

        # 删除条目
        del self._entries[entry_id]

        # 从用户条目列表中移除
        if user_id in self._user_entries:
            self._user_entries[user_id] = [
                eid for eid in self._user_entries[user_id]
                if eid != entry_id
            ]

        # 删除 embedding
        self._embeddings.pop(entry_id, None)

        # 从向量数据库删除
        if self.vector_store:
            self.vector_store.delete(entry_id)

        logger.info(f"删除档案记忆: {entry_id}")
        return True

    def clear_user_entries(self, user_id: str) -> int:
        """清空用户的所有条目"""
        entry_ids = self._user_entries.get(user_id, [])
        count = 0

        for entry_id in entry_ids:
            if entry_id in self._entries:
                del self._entries[entry_id]
                self._embeddings.pop(entry_id, None)
                count += 1

        self._user_entries[user_id] = []

        if self.vector_store:
            self.vector_store.delete_by_filter({"user_id": user_id})

        logger.info(f"清空用户档案: {user_id}, 删除 {count} 条")
        return count

    def get_context_string(
        self,
        user_id: str,
        query: str,
        top_k: int = 3,
    ) -> str:
        """获取档案记忆上下文"""
        entries = self.search(query, user_id, top_k)
        if not entries:
            return ""

        parts = ["## 相关记忆"]
        for i, entry in enumerate(entries, 1):
            category = f"[{entry.category}] " if entry.category else ""
            tags = f" ({', '.join(entry.tags)})" if entry.tags else ""
            parts.append(f"{i}. {category}{entry.content}{tags}")

        return "\n".join(parts)

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
