"""
核心记忆 (Core Memory)

存储始终在上下文中的信息：
- 用户信息（姓名、偏好）
- Agent 人设
- 当前目标/任务

特点：
- 始终在上下文中
- 容量有限
- 快速读写
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from loguru import logger


@dataclass
class UserProfile:
    """用户画像"""
    user_id: str
    name: str = ""
    preferences: Dict[str, Any] = field(default_factory=dict)
    learning_style: str = ""  # visual, reading, practice
    weak_topics: List[str] = field(default_factory=list)
    strong_topics: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "preferences": self.preferences,
            "learning_style": self.learning_style,
            "weak_topics": self.weak_topics,
            "strong_topics": self.strong_topics,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_context_string(self) -> str:
        """转换为上下文字符串"""
        parts = []
        if self.name:
            parts.append(f"用户名: {self.name}")
        if self.learning_style:
            parts.append(f"学习风格: {self.learning_style}")
        if self.weak_topics:
            parts.append(f"薄弱知识点: {', '.join(self.weak_topics)}")
        if self.strong_topics:
            parts.append(f"擅长领域: {', '.join(self.strong_topics)}")
        return "\n".join(parts)


@dataclass
class AgentPersona:
    """Agent 人设"""
    name: str = "知识助手"
    role: str = "专业的技术问答助手"
    personality: str = "友好、专业、耐心"
    rules: List[str] = field(default_factory=lambda: [
        "基于知识库回答，不编造信息",
        "引用来源时使用 [1]、[2] 标记",
        "如果不确定，明确说明",
    ])

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "role": self.role,
            "personality": self.personality,
            "rules": self.rules,
        }

    def to_context_string(self) -> str:
        """转换为上下文字符串"""
        parts = [
            f"你是{self.name}，{self.role}。",
            f"性格特点：{self.personality}。",
            "规则：",
        ]
        for i, rule in enumerate(self.rules, 1):
            parts.append(f"  {i}. {rule}")
        return "\n".join(parts)


class CoreMemory:
    """
    核心记忆管理器

    管理用户画像和 Agent 人设，
    这些信息始终在上下文中。
    """

    def __init__(self):
        self._user_profiles: Dict[str, UserProfile] = {}
        self._agent_persona = AgentPersona()

    def get_user_profile(self, user_id: str) -> UserProfile:
        """获取用户画像"""
        if user_id not in self._user_profiles:
            self._user_profiles[user_id] = UserProfile(user_id=user_id)
        return self._user_profiles[user_id]

    def update_user_profile(self, user_id: str, **kwargs) -> UserProfile:
        """更新用户画像"""
        profile = self.get_user_profile(user_id)

        for key, value in kwargs.items():
            if hasattr(profile, key):
                setattr(profile, key, value)

        profile.updated_at = datetime.now().isoformat()
        logger.info(f"更新用户画像: {user_id} - {kwargs.keys()}")
        return profile

    def add_weak_topic(self, user_id: str, topic: str):
        """添加薄弱知识点"""
        profile = self.get_user_profile(user_id)
        if topic not in profile.weak_topics:
            profile.weak_topics.append(topic)
            profile.updated_at = datetime.now().isoformat()
            logger.info(f"添加薄弱知识点: {user_id} - {topic}")

    def add_strong_topic(self, user_id: str, topic: str):
        """添加擅长领域"""
        profile = self.get_user_profile(user_id)
        if topic not in profile.strong_topics:
            profile.strong_topics.append(topic)
            profile.updated_at = datetime.now().isoformat()
            logger.info(f"添加擅长领域: {user_id} - {topic}")

    def get_agent_persona(self) -> AgentPersona:
        """获取 Agent 人设"""
        return self._agent_persona

    def update_agent_persona(self, **kwargs):
        """更新 Agent 人设"""
        for key, value in kwargs.items():
            if hasattr(self._agent_persona, key):
                setattr(self._agent_persona, key, value)
        logger.info(f"更新 Agent 人设: {kwargs.keys()}")

    def get_context(self, user_id: str) -> str:
        """获取核心记忆上下文"""
        parts = []

        # Agent 人设
        parts.append("## AI 助手信息")
        parts.append(self._agent_persona.to_context_string())

        # 用户画像
        profile = self.get_user_profile(user_id)
        user_context = profile.to_context_string()
        if user_context:
            parts.append("\n## 用户信息")
            parts.append(user_context)

        return "\n".join(parts)

    def get_all_profiles(self) -> List[UserProfile]:
        """获取所有用户画像"""
        return list(self._user_profiles.values())

    def delete_user(self, user_id: str) -> bool:
        """删除用户"""
        if user_id in self._user_profiles:
            del self._user_profiles[user_id]
            logger.info(f"删除用户: {user_id}")
            return True
        return False
