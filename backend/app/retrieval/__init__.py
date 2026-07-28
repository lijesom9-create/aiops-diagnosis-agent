"""
Retrieval Layer - 检索层

职责分离：
- Memory Layer（memory/）：负责"存什么" — Store / Update / Forget
- Retrieval Layer（本模块）：负责"怎么查" — Retrieve

检索方式：
- Sparse Retrieval：稀疏检索 (BM25)
- Dense Retrieval：稠密检索 (Vector)
- Hybrid Retrieval：混合检索 (Sparse + Dense + Fusion)
- Reranking：重排序 (CrossEncoder / LLM)
"""

from .base import BaseRetriever, RetrievalResult
from .reranker import Reranker

# 新增：混合检索器
from .hybrid_retriever import (
    SparseRetriever,
    DenseRetriever,
    HybridRetriever,
    QueryRewriter,
)

__all__ = [
    # 基类
    "BaseRetriever",
    "RetrievalResult",

    # 重排序
    "Reranker",

    # 混合检索器
    "SparseRetriever",
    "DenseRetriever",
    "HybridRetriever",
    "QueryRewriter",
]
