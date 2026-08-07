"""
Memory Layer - 记忆管理层

职责分离：
- Memory Layer（本模块）：负责"存什么" — Store / Update / Forget
- Retrieval Layer（retrieval/）：负责"怎么查" — Retrieve

记忆类型（参考 MemGPT/Letta 架构）：
- CoreMemory: 核心记忆（用户画像、Agent 人设）- 始终在上下文中
- RecallMemory: 回忆记忆（对话历史）- 支持语义搜索
- ArchivalMemory: 档案记忆（用户笔记、学习记录）- 大容量存储
- MemoryManager: 记忆管理器（整合所有记忆层次）

注：RAG 生成由 LangGraphAgent（app/langgraph_agent/）统一承载，
    评测脚本使用各自内联的轻量生成逻辑，不再保留独立 RAGGenerator 类。
"""

# 新的记忆系统
from .core_memory import CoreMemory, UserProfile, AgentPersona
from .recall_memory import RecallMemory, ConversationTurn, ConversationSession
from .archival_memory import ArchivalMemory, MemoryEntry
from .memory_manager import MemoryManager

__all__ = [
    # 核心记忆
    "CoreMemory",
    "UserProfile",
    "AgentPersona",

    # 回忆记忆
    "RecallMemory",
    "ConversationTurn",
    "ConversationSession",

    # 档案记忆
    "ArchivalMemory",
    "MemoryEntry",

    # 记忆管理器
    "MemoryManager",
]
