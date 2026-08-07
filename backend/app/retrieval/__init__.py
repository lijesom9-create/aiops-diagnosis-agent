"""
Retrieval Layer - 检索层

职责分离：
- Memory Layer（memory/）：负责"存什么" — Store / Update / Forget
- Retrieval Layer（本模块）：负责"怎么查" — Retrieve

检索方式：
- Dense Retrieval：稠密检索（Vector，Qdrant）
- Reranking：重排序 (CrossEncoder / LLM)

注：早期测试对比用的 hybrid_retriever（SparseRetriever/DenseRetriever/HybridRetriever）
已移除，线上检索统一走 knowledge/unified_store.hybrid_search_parent_child。
"""

from .base import BaseRetriever, RetrievalResult
from .reranker import Reranker

__all__ = [
    # 基类
    "BaseRetriever",
    "RetrievalResult",

    # 重排序
    "Reranker",
]
