"""
Knowledge Base Management API - 知识库管理

提供向量数据库的运维能力：
- 统计信息（集合大小、文档分布、BM25 索引状态）
- 文档级统计（每个文档的块数、字符数）
- 集合管理（清空、重建索引）
"""

from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from loguru import logger

from ..core.auth import get_current_user, UserResponse


router = APIRouter(prefix="/api/knowledge", tags=["知识库管理"])

_knowledge_store = None


def set_knowledge_store(store):
    """设置知识存储（由 main.py 调用）"""
    global _knowledge_store
    _knowledge_store = store


def _get_store():
    """获取知识存储实例"""
    if _knowledge_store is not None:
        return _knowledge_store
    from ..shared_services import get_knowledge_store
    ks = get_knowledge_store()
    if not ks:
        raise RuntimeError("KnowledgeStore 未初始化")
    return ks


# ========== 响应模型 ==========

class CollectionStats(BaseModel):
    """单个集合统计"""
    name: str
    size: int
    dimension: Optional[int] = None


class DocumentStat(BaseModel):
    """文档级统计"""
    document_id: str
    filename: str
    chunk_count: int
    child_count: int = 0
    parent_count: int = 0
    char_count: int = 0


class KnowledgeStatsResponse(BaseModel):
    """知识库统计响应"""
    total_records: int
    child_records: int
    parent_records: int
    clip_image_records: int
    sparse_enabled: bool
    document_count: int
    documents: List[DocumentStat]
    collections: List[CollectionStats]


# ========== API 端点 ==========

@router.get("/stats", response_model=KnowledgeStatsResponse)
async def get_knowledge_stats(
    current_user: UserResponse = Depends(get_current_user),
):
    """
    获取知识库统计信息

    返回：
    - 各集合记录数（child / parent / clip_image）
    - sparse vector 是否启用
    - 按文档聚合的块数统计
    """
    try:
        store = _get_store()

        # 各集合大小
        child_size = store.vector_store.size()
        parent_size = store._parent_store.size() if store._separate_parent_child else 0
        total_size = child_size + parent_size

        # CLIP 图片集合
        clip_size = 0
        clip_dim = None
        if hasattr(store, '_clip_image_store') and store._clip_image_store is not None:
            try:
                clip_size = store._clip_image_store.size()
                clip_dim = getattr(store._clip_image_store, '_dimension', None)
            except Exception:
                pass

        # sparse vector 状态（BGE-M3 同源 sparse，替代自研 BM25）
        sparse_enabled = getattr(store.vector_store, '_has_sparse', False)

        # 按文档聚合统计
        doc_stats = _aggregate_document_stats(store)

        collections = [
            CollectionStats(name="knowledge (child)", size=child_size, dimension=getattr(store.vector_store, '_dimension', None)),
            CollectionStats(name="knowledge_parent", size=parent_size, dimension=getattr(store._parent_store, '_dimension', None) if store._separate_parent_child else None),
        ]
        if clip_size > 0:
            collections.append(CollectionStats(name="knowledge_clip_image", size=clip_size, dimension=clip_dim))

        return KnowledgeStatsResponse(
            total_records=total_size + clip_size,
            child_records=child_size,
            parent_records=parent_size,
            clip_image_records=clip_size,
            sparse_enabled=sparse_enabled,
            document_count=len(doc_stats),
            documents=doc_stats,
            collections=collections,
        )

    except Exception as e:
        logger.error(f"获取知识库统计失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取知识库统计失败: {str(e)}"
        )


@router.get("/documents/{document_id}")
async def get_document_chunks(
    document_id: str,
    current_user: UserResponse = Depends(get_current_user),
):
    """
    查询指定文档的所有知识块

    返回该文档在向量库中的所有 chunk（父子块合并）。
    """
    try:
        store = _get_store()
        chunks = store.get_by_document(document_id)

        if not chunks:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"文档 {document_id} 无知识块或不存在"
            )

        return {
            "document_id": document_id,
            "total_chunks": len(chunks),
            "chunks": [
                {
                    "id": c.get("id", ""),
                    "content": (c.get("content") or c.get("document", ""))[:200],
                    "metadata": c.get("metadata", {}),
                }
                for c in chunks
            ],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询文档知识块失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"查询文档知识块失败: {str(e)}"
        )


@router.delete("/documents/{document_id}")
async def delete_document_from_knowledge(
    document_id: str,
    current_user: UserResponse = Depends(get_current_user),
):
    """
    从知识库删除文档的所有向量数据

    仅删除向量库中的数据，不删除 DB 文档记录和图片文件。
    用于向量数据修复场景。
    """
    try:
        store = _get_store()
        deleted = store.delete_by_document(document_id)

        logger.info(f"从知识库删除文档向量: {document_id}, 删除 {deleted} 条")

        return {
            "message": "文档向量数据已删除",
            "document_id": document_id,
            "deleted_count": deleted,
        }

    except Exception as e:
        logger.error(f"删除文档向量失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除文档向量失败: {str(e)}"
        )


# ========== 辅助函数 ==========

def _aggregate_document_stats(store) -> List[DocumentStat]:
    """按 document_id 聚合统计各文档的块数"""
    from collections import defaultdict

    doc_chunks: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"filename": "", "child_count": 0, "parent_count": 0, "char_count": 0}
    )

    # 从 child store 聚合
    try:
        child_data = store.vector_store.get_all(include=["metadatas"])
        for meta in child_data.get("metadatas", []):
            doc_id = meta.get("document_id", "")
            if not doc_id:
                continue
            doc_chunks[doc_id]["filename"] = meta.get("filename", meta.get("title", ""))
            doc_chunks[doc_id]["child_count"] += 1
    except Exception as e:
        logger.debug(f"聚合 child store 统计失败: {e}")

    # 从 parent store 聚合
    if store._separate_parent_child:
        try:
            parent_data = store._parent_store.get_all(include=["metadatas"])
            for meta in parent_data.get("metadatas", []):
                doc_id = meta.get("document_id", "")
                if not doc_id:
                    continue
                doc_chunks[doc_id]["filename"] = meta.get("filename", meta.get("title", ""))
                doc_chunks[doc_id]["parent_count"] += 1
        except Exception as e:
            logger.debug(f"聚合 parent store 统计失败: {e}")

    # 构建响应
    result: List[DocumentStat] = []
    for doc_id, info in sorted(doc_chunks.items(), key=lambda x: x[1]["child_count"], reverse=True):
        result.append(DocumentStat(
            document_id=doc_id,
            filename=info["filename"],
            chunk_count=info["child_count"] + info["parent_count"],
            child_count=info["child_count"],
            parent_count=info["parent_count"],
            char_count=info["char_count"],
        ))

    return result
