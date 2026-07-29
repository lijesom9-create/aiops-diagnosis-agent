"""
Document API - 文档管理

支持 PDF/Word/TXT/Markdown 上传，解析、分块、向量化。
"""

import uuid
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, BackgroundTasks
from pydantic import BaseModel
from loguru import logger

from ..core.auth import get_current_user, UserResponse
from ..core.database import get_db, Database
from ..document.uploader import DocumentUploader
from ..models.document import Document, DocumentStatus


router = APIRouter(prefix="/api/documents", tags=["文档管理"])


def _fix_encoding(text: str) -> str:
    """修复编码问题：尝试多种编码方式修复乱码"""
    if not text:
        return text

    # 尝试 latin-1 -> UTF-8
    try:
        result = text.encode('latin-1').decode('utf-8')
        if result != text:
            return result
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    # 尝试 latin-1 -> GBK
    try:
        result = text.encode('latin-1').decode('gbk')
        if result != text:
            return result
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    # 尝试直接用 GBK 解码（如果已经是 GBK 编码的 bytes 被错误解码）
    try:
        # 检查是否包含 GBK 特征的乱码字符
        if any('\x80' <= c <= '\xff' for c in text):
            return text.encode('latin-1').decode('utf-8', errors='replace')
    except Exception:
        pass

    return text


# ========== 请求/响应模型 ==========

class DocumentResponse(BaseModel):
    """文档响应"""
    document_id: str
    filename: str
    title: str
    status: str
    chunk_count: int
    char_count: int
    created_at: str


class DocumentListResponse(BaseModel):
    """文档列表响应"""
    documents: List[DocumentResponse]
    total: int


# ========== 辅助函数 ==========

_uploader: Optional[DocumentUploader] = None
_knowledge_store = None


def set_knowledge_store(store):
    """设置知识存储（由 main.py 调用）"""
    global _knowledge_store
    _knowledge_store = store


def get_document_uploader() -> DocumentUploader:
    """获取文档上传服务实例"""
    global _uploader
    if _uploader is None:
        # 延迟获取 knowledge_store，确保已初始化
        from ..shared_services import get_knowledge_store
        ks = _knowledge_store or get_knowledge_store()
        if not ks:
            raise RuntimeError("KnowledgeStore 未初始化，无法创建 DocumentUploader")
        _uploader = DocumentUploader(knowledge_store=ks)
    return _uploader


# ========== API 端点 ==========

@router.get("/", response_model=DocumentListResponse)
async def list_documents(
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    获取文档列表

    获取当前用户的所有文档。
    """
    try:
        user_id = current_user.user_id
        documents = await db.get_user_documents(user_id)

        doc_list = []
        for doc in documents:
            created_at = doc.get("created_at", "")
            if isinstance(created_at, datetime):
                created_at = created_at.isoformat()
            doc_list.append(DocumentResponse(
                document_id=doc.get("document_id", ""),
                filename=doc.get("filename", ""),
                title=doc.get("title", ""),
                status=doc.get("status", "pending"),
                chunk_count=doc.get("chunk_count", 0),
                char_count=doc.get("char_count", 0),
                created_at=str(created_at),
            ))

        return DocumentListResponse(
            documents=doc_list,
            total=len(doc_list),
        )

    except Exception as e:
        logger.error(f"获取文档列表失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取文档列表失败: {str(e)}"
        )


@router.post("/upload", response_model=DocumentResponse)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    title_utf8: Optional[str] = None,  # UTF-8 编码的标题（用于修复中文乱码）
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    上传文档

    支持 PDF、Word、TXT、Markdown 格式。
    """
    try:
        user_id = current_user.user_id

        # 读取文件内容
        content = await file.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="文件内容为空"
            )

        # 修复文件名和标题的编码问题
        raw_filename = file.filename or "unnamed"
        filename = _fix_encoding(raw_filename)

        # 优先使用 UTF-8 编码的标题
        if title_utf8:
            title = title_utf8
        elif title:
            title = _fix_encoding(title)

        doc_type = filename.split(".")[-1].lower() if "." in filename else ""

        # 检查文件类型
        supported_types = {"pdf", "docx", "txt", "md", "markdown"}
        if doc_type not in supported_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支持的文件类型: {doc_type}，支持: {', '.join(supported_types)}"
            )

        # 创建文档记录
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        document = {
            "document_id": document_id,
            "user_id": user_id,
            "filename": filename,
            "title": title or filename,
            "doc_type": doc_type,
            "status": DocumentStatus.PENDING.value,
            "chunk_count": 0,
            "char_count": 0,
            "created_at": now,
        }

        await db.create_document(document)

        # 后台异步处理
        uploader = get_document_uploader()
        background_tasks.add_task(
            _process_document,
            db, uploader, document_id, content, filename, title, user_id,
        )

        logger.info(f"文档上传成功: {document_id} - {filename}")

        return DocumentResponse(
            document_id=document_id,
            filename=filename,
            title=title or filename,
            status=DocumentStatus.PENDING.value,
            chunk_count=0,
            char_count=0,
            created_at=now,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文档上传失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"文档上传失败: {str(e)}"
        )


@router.get("/{document_id}/status")
async def get_document_status(
    document_id: str,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    获取文档状态

    查询文档处理状态。
    """
    try:
        user_id = current_user.user_id
        doc = await db.get_document(document_id)

        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档不存在"
            )

        if doc.get("user_id") != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此文档"
            )

        return {
            "document_id": doc.get("document_id"),
            "status": doc.get("status"),
            "chunk_count": doc.get("chunk_count", 0),
            "char_count": doc.get("char_count", 0),
            "error_message": doc.get("error_message", ""),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取文档状态失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取文档状态失败: {str(e)}"
        )


@router.delete("/{document_id}")
async def delete_document(
    document_id: str,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    删除文档

    删除文档及其向量数据。
    """
    try:
        user_id = current_user.user_id
        doc = await db.get_document(document_id)

        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档不存在"
            )

        if doc.get("user_id") != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权删除此文档"
            )

        # 删除向量数据
        if _knowledge_store:
            try:
                _knowledge_store.delete_by_document(document_id)
            except Exception as e:
                logger.warning(f"删除向量数据失败: {e}")

        # 删除文档对应的图片（多模态 RAG）
        try:
            from ..document.image_store import get_image_store
            get_image_store().delete_document_images(document_id)
        except Exception as e:
            logger.warning(f"删除文档图片失败: {e}")

        # 删除文档记录
        await db.delete_document(document_id)

        logger.info(f"文档删除成功: {document_id}")

        return {"message": "文档已删除"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文档删除失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"文档删除失败: {str(e)}"
        )


# ========== 多模态 RAG：图片访问 ==========

@router.get("/images/{document_id}/{image_name}")
async def get_image(
    document_id: str,
    image_name: str,
    current_user: UserResponse = Depends(get_current_user),
):
    """
    获取文档中的图片（多模态 RAG）

    路径参数：
    - document_id: 文档 ID
    - image_name: 图片文件名（如 img_0001.png）

    返回图片二进制流，用于前端在聊天答案中展示原图引用。
    """
    # 简单的安全校验：禁止路径穿越
    if "/" in image_name or "\\" in image_name or ".." in image_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的图片名"
        )

    from ..document.image_store import get_image_store
    from fastapi.responses import Response

    relative_path = f"{document_id}/{image_name}"
    image_bytes = get_image_store().read_bytes(relative_path)
    if image_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="图片不存在"
        )

    # 推断 MIME
    ext = image_name.rsplit(".", 1)[-1].lower() if "." in image_name else "png"
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }
    media_type = mime_map.get(ext, "image/png")

    return Response(content=image_bytes, media_type=media_type)


# ========== 后台处理 ==========

async def _process_document(
    db: Database,
    uploader: DocumentUploader,
    document_id: str,
    content: bytes,
    filename: str,
    title: Optional[str],
    user_id: str,
):
    """后台处理文档：解析、分块、向量化"""
    try:
        await db.update_document(document_id, {"status": DocumentStatus.PROCESSING.value})

        result = await uploader.upload(
            content=content,
            filename=filename,
            title=title,
            user_id=user_id,
            document_id=document_id,
        )

        await db.update_document(document_id, {
            "status": DocumentStatus.COMPLETED.value,
            "chunk_count": result.get("chunk_count", 0),
            "char_count": result.get("char_count", 0),
        })

        logger.info(f"文档处理完成: {document_id} - {filename}")

    except Exception as e:
        logger.error(f"文档处理失败: {document_id} - {filename}, {e}")
        await db.update_document(document_id, {
            "status": DocumentStatus.FAILED.value,
            "error_message": str(e),
        })
