"""
Retrieval Base - 检索基类
定义检索接口
"""

from abc import ABC, abstractmethod
from typing import Dict, List


class RetrievalResult:
    """检索结果"""

    def __init__(
        self,
        content: str,
        source: str,
        score: float = 0.0,
        metadata: Dict = None,
        doc_id: str = "",
    ):
        self.doc_id = doc_id
        self.content = content
        self.source = source
        self.score = score
        self.metadata = metadata or {}

    def to_dict(self) -> Dict:
        return {
            "doc_id": self.doc_id,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "metadata": self.metadata,
        }


class BaseRetriever(ABC):
    """
    检索基类

    职责：
    - 定义检索接口
    - 提供统一的检索方法

    设计原则：
    - 检索与数据分离
    - 支持多种检索方式
    - 可组合使用
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """检索器名称"""
        pass

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        limit: int = 5,
        filters: Dict = None,
    ) -> List[RetrievalResult]:
        """
        执行检索

        Args:
            query: 查询文本
            limit: 返回数量限制
            filters: 过滤条件

        Returns:
            List[RetrievalResult]: 检索结果
        """
        pass
