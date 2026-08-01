"""
Document - 用户上传的学习资料

跟踪资料的处理状态和元数据，支持异步解析和向量化。
"""

from typing import Optional, List
from datetime import datetime
from enum import Enum
import uuid


class BaseModel:
    """基础模型类，提供 id 和时间戳"""

    def __init__(self, id: Optional[str] = None):
        self.id = id or str(uuid.uuid4())
        self.created_at = datetime.now()
        self.updated_at = datetime.now()

    def touch(self):
        self.updated_at = datetime.now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class DocumentStatus(str, Enum):
    """文档处理状态"""
    PENDING = "pending"       # 等待处理
    PROCESSING = "processing" # 正在解析
    COMPLETED = "completed"   # 处理完成
    FAILED = "failed"         # 处理失败


class DocumentCategory(str, Enum):
    """文档分类（企业技术知识库）"""
    API_DOC = "api_doc"              # API 文档
    OPS_SOP = "ops_sop"              # 运维 SOP / 故障排查
    ARCHITECTURE = "architecture"    # 架构设计
    DEV_GUIDE = "dev_guide"          # 开发指南 / 编码规范
    TROUBLESHOOTING = "troubleshooting"  # 故障排查
    OTHER = "other"                  # 其他


class Document(BaseModel):
    """
    用户上传的学习资料

    与 Topic 绑定，记录处理状态以便前端轮询。
    """

    def __init__(
        self,
        document_id: Optional[str] = None,
        user_id: str = "",
        topic_id: str = "",
        filename: str = "",
        title: str = "",
        doc_type: str = "",
        source_url: str = "",
        status: DocumentStatus = DocumentStatus.PENDING,
        chunk_count: int = 0,
        char_count: int = 0,
        error_message: str = "",
        category: str = "other",
        tags: Optional[List[str]] = None,
        version: int = 1,
    ):
        super().__init__(id=document_id)
        self.user_id = user_id
        self.topic_id = topic_id
        self.filename = filename
        self.title = title
        self.doc_type = doc_type
        self.source_url = source_url
        self.status = status
        self.chunk_count = chunk_count
        self.char_count = char_count
        self.error_message = error_message
        self.category = category
        self.tags = tags or []
        self.version = version

    def to_dict(self) -> dict:
        """转换为字典"""
        base = super().to_dict()
        base.update({
            "document_id": self.id,
            "user_id": self.user_id,
            "topic_id": self.topic_id,
            "filename": self.filename,
            "title": self.title,
            "doc_type": self.doc_type,
            "source_url": self.source_url,
            "status": self.status.value if isinstance(self.status, Enum) else self.status,
            "chunk_count": self.chunk_count,
            "char_count": self.char_count,
            "error_message": self.error_message,
            "category": self.category,
            "tags": self.tags,
            "version": self.version,
        })
        return base

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        """从字典创建"""
        doc = cls(
            document_id=data.get("id") or data.get("document_id"),
            user_id=data.get("user_id", ""),
            topic_id=data.get("topic_id", ""),
            filename=data.get("filename", ""),
            title=data.get("title", ""),
            doc_type=data.get("doc_type", ""),
            source_url=data.get("source_url", ""),
            status=data.get("status", DocumentStatus.PENDING),
            chunk_count=data.get("chunk_count", 0),
            char_count=data.get("char_count", 0),
            error_message=data.get("error_message", ""),
            category=data.get("category", "other"),
            tags=data.get("tags", []),
            version=data.get("version", 1),
        )
        if "created_at" in data:
            doc.created_at = datetime.fromisoformat(data["created_at"])
        if "updated_at" in data:
            doc.updated_at = datetime.fromisoformat(data["updated_at"])
        return doc
