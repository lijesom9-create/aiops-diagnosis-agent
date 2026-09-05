"""知识条目数据模型"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict


@dataclass
class KnowledgeItem:
    """知识条目"""
    id: str
    title: str
    content: str
    source: str  # course | teaching | user_document
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    org_id: str = ""

    def to_chroma(self) -> Dict[str, Any]:
        """转换为 ChromaDB 存储格式"""
        return {
            "id": self.id,
            "content": self.content,
            "metadata": {
                "title": self.title,
                "source": self.source,
                "created_at": self.created_at,
                "org_id": self.org_id,
                **self.metadata,
            }
        }
