"""
Memory Layer - 记忆管理层

职责分离：
- Memory Layer（本模块）：负责"存什么" — Store / Update / Forget
- Retrieval Layer（retrieval/）：负责"怎么查" — Retrieve

记忆类型（参考 MemGPT/Letta 架构；用户画像层已随教学域聚焦裁剪移除）：
- RecallMemory: 回忆记忆（对话历史）- 支持语义搜索
- ArchivalMemory: 档案记忆（诊断结论归档、重要信息）- 大容量存储
- MemoryManager: 记忆管理器（整合记忆层次 + RAG 上下文组装）

注：RAG 生成由 LangGraphAgent（app/langgraph_agent/）统一承载，
    评测脚本使用各自内联的轻量生成逻辑，不再保留独立 RAGGenerator 类。
"""

from .archival_memory import ArchivalMemory, MemoryEntry
from .memory_manager import MemoryManager
from .recall_memory import ConversationSession, ConversationTurn, RecallMemory

__all__ = [
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
