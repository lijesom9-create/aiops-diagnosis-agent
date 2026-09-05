"""
Document API - 文档管理（薄 handler 层）

支持 PDF/Word/TXT/Markdown 上传，解析、分块、向量化。
包含：去重检查、批量入库、文档更新（先删后加）。

分层：业务逻辑（文件名/编码安全处理、分类校验、哈希去重、frontmatter 解析、
公共文档记录构造、处理投递、产物清理、后台解析流水线）已下沉到
services/document_service.py（T10）。本文件仅保留：
- 端点路由与依赖注入（get_current_user / require_admin / get_db / BackgroundTasks）
- HTTP 请求/响应转换与 HTTPException 状态码映射
- 图片访问端点（含路径逃逸三层安全校验，安全敏感代码整体留在 HTTP 层）
- 对外符号转发（main.py / 测试依赖 app.api.documents 的符号保持不变）
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from loguru import logger

from ..core.auth import UserResponse, get_current_user, require_admin
from ..core.config import settings
from ..core.database import Database, get_db
from ..models.document import DocumentStatus
from ..services.document_service import (
    # 向后兼容转发（main / 潜在调用方依赖 app.api.documents 的符号）
    _ALLOWED_EXTENSIONS,
    # handler 直接依赖
    BatchUploadResponse,
    DocumentListResponse,
    DocumentResponse,
    _check_duplicate,
    _compute_file_hash,
    _extract_extension,
    _fix_encoding,
    _process_document,
    _sanitize_filename,
    build_document_record,
    extract_biz_meta,
    get_document_uploader,
    normalize_category,
    purge_document_artifacts,
    set_knowledge_store,
    submit_document_processing,
)

router = APIRouter(prefix="/api/documents", tags=["文档管理"])

__all__ = [
    # 端点
    "router",
    "list_documents", "upload_document", "get_document_status", "retry_document",
    "delete_document", "update_document", "batch_upload_documents", "get_image",
    # 模型
    "DocumentResponse", "DocumentListResponse", "BatchUploadResponse",
    # 转发：业务符号（业务已下沉，main / 测试依赖 app.api.documents 的符号）
    "_ALLOWED_EXTENSIONS", "_check_duplicate", "_compute_file_hash",
    "_extract_extension", "_fix_encoding", "_process_document", "_sanitize_filename",
    "build_document_record", "extract_biz_meta", "get_document_uploader",
    "normalize_category", "purge_document_artifacts", "set_knowledge_store",
    "submit_document_processing",
]


# ========== API 端点 ==========

@router.get("/", response_model=DocumentListResponse)
async def list_documents(
    category: Optional[str] = None,
    current_user: UserResponse = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """
    获取文档列表

    获取当前用户的所有文档，支持按分类过滤。
    管理员可查看全部文档；普通用户仅查看自己的文档 + 公共文档。
    """
    try:
        # admin 看全部文档；普通用户看自己的 + 公共文档
        if current_user.role == "admin":
            documents = await db.get_all_documents()
        else:
            documents = await db.get_user_documents(current_user.user_id)

        # 按分类过滤
        if category:
            documents = [d for d in documents if d.get("category", "other") == category]

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
                category=doc.get("category", "other"),
                tags=doc.get("tags", []),
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
    skip_duplicate: bool = Form(True),  # 默认开启去重
    category: str = Form("other"),  # 文档分类
    shared_to_diagnosis: bool = Form(True),  # 共享给自动诊断（旁路组织隔离，敏感文档可关闭）
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """
    上传文档（仅管理员）

    支持 PDF、Word、TXT、Markdown 格式。
    默认开启文件哈希去重（skip_duplicate=True），相同文件不重复入库。
    管理员导入的文档默认为公共文档（所有用户可检索、可问答）。
    """
    try:
        # admin 导入的文档为公共文档：user_id 留空（检索层据此判定公共）
        # uploaded_by 记录导入者，便于审计
        admin_user_id = current_user.user_id

        # 校验分类
        category = normalize_category(category)

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
        # 安全校验：清理文件名（去路径分隔符、控制字符、限长）
        filename = _sanitize_filename(filename)

        # 优先使用 UTF-8 编码的标题
        if title_utf8:
            title = title_utf8
        elif title:
            title = _fix_encoding(title)

        # 安全提取扩展名并校验（白名单）
        doc_type = _extract_extension(filename)
        if doc_type not in _ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支持的文件类型: {doc_type or '未知'}，支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
            )

        # 解析 Markdown YAML frontmatter → 运维业务 metadata（doc_type/service/severity 等）
        biz_meta, doc_type = extract_biz_meta(content, shared_to_diagnosis, doc_type)

        # 去重检查：公共文档全局去重（user_id="" 命中所有公共文档）
        file_hash = _compute_file_hash(content)
        if skip_duplicate:
            existing = await _check_duplicate(db, "", file_hash)
            if existing:
                logger.info(f"文件重复，跳过入库: {filename} -> 已存在 {existing.get('document_id')}")
                return DocumentResponse(
                    document_id=existing.get("document_id", ""),
                    filename=existing.get("filename", filename),
                    title=existing.get("title", title or filename),
                    status=existing.get("status", "completed"),
                    chunk_count=existing.get("chunk_count", 0),
                    char_count=existing.get("char_count", 0),
                    created_at=str(existing.get("created_at", "")),
                    category=existing.get("category", "other"),
                    tags=existing.get("tags", []),
                )

        # 创建文档记录
        document_id, document = build_document_record(
            admin_user_id=admin_user_id,
            filename=filename,
            title=title or filename,
            doc_type=doc_type,
            file_hash=file_hash,
            biz_meta=biz_meta,
            category=category,
            tags=[],
        )

        await db.create_document(document)

        # 异步处理：USE_CELERY=True 走 Celery worker；False 降级到 BackgroundTasks 同步处理
        await submit_document_processing(
            db, background_tasks,
            document_id=document_id, content=content, filename=filename,
            title=title, admin_user_id=admin_user_id, biz_meta=biz_meta,
            audit_log=True,
        )

        return DocumentResponse(
            document_id=document_id,
            filename=filename,
            title=title or filename,
            status=DocumentStatus.PENDING.value,
            chunk_count=0,
            char_count=0,
            created_at=document["created_at"],
            category=category,
            tags=[],
        )

    except HTTPException:
        raise
    except Exception as e:
        # 内部异常详情只记日志，不返回给客户端（防止泄露文件路径/库内部信息）
        logger.exception(f"文档上传失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="文档上传失败，请稍后重试或联系管理员"
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
        doc = await db.get_document(document_id)

        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档不存在"
            )

        # admin 可查任意文档；普通用户仅查自己的 + 公共文档
        if current_user.role != "admin":
            doc_user_id = doc.get("user_id", "")
            is_public = doc.get("is_public", False)
            if doc_user_id and doc_user_id != current_user.user_id and not is_public:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="无权访问此文档"
                )

        result = {
            "document_id": doc.get("document_id"),
            "status": doc.get("status"),
            "chunk_count": doc.get("chunk_count", 0),
            "char_count": doc.get("char_count", 0),
            "error_message": doc.get("error_message", ""),
            "task_id": doc.get("task_id", ""),
        }

        # Celery 模式下补充 task 实时状态（PENDING/STARTED/SUCCESS/FAILURE/RETRY）
        if settings.USE_CELERY and doc.get("task_id"):
            try:
                from app.celery_app import app as celery_app
                async_result = celery_app.AsyncResult(doc["task_id"])
                result["task_state"] = async_result.state
            except Exception as e:
                result["task_state"] = "UNKNOWN"
                logger.warning(f"查询 Celery task 状态失败: {e}")

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取文档状态失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取文档状态失败: {str(e)}"
        )


@router.post("/{document_id}/retry")
async def retry_document(
    document_id: str,
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """
    重试文档处理（仅管理员，需 USE_CELERY=True）

    重新投递 Celery task 处理文档。要求文档有 file_path（Celery 模式上传时存储）。
    """
    if not settings.USE_CELERY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前未启用 Celery 异步处理（USE_CELERY=False），无法重试"
        )

    try:
        doc = await db.get_document(document_id)
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档不存在"
            )

        if doc.get("status") not in ("failed", "completed"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"当前状态 {doc.get('status')} 不可重试，仅 failed/completed 可重试"
            )

        file_path = doc.get("file_path")
        if not file_path or not Path(file_path).exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="原始文件不存在，无法重试"
            )

        from app.tasks.document_tasks import process_document
        # 从文档记录取回业务 metadata（frontmatter 解析的 doc_type/service 等）：
        # 重试不回传会导致重建的 chunk 丢失业务元数据，Agent 的精准过滤将命中不到
        retry_meta = doc.get("extra_metadata") or None
        task = process_document.delay(
            document_id, file_path, doc.get("filename", ""), doc.get("title", ""),
            extra_metadata=retry_meta,
        )
        await db.update_document(document_id, {
            "status": DocumentStatus.PENDING.value,
            "task_id": task.id,
            "error_message": "",
        })
        logger.info(f"文档重试已投递: {document_id} task={task.id}")
        return {"document_id": document_id, "task_id": task.id, "status": "pending"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"文档重试失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="文档重试失败"
        )


@router.delete("/{document_id}")
async def delete_document(
    document_id: str,
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """
    删除文档（仅管理员）

    删除文档及其向量数据。管理员可删除任意文档。
    """
    try:
        doc = await db.get_document(document_id)

        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档不存在"
            )

        # 删除向量数据 + 图片（业务下沉 document_service）
        await purge_document_artifacts(document_id)

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


@router.put("/{document_id}", response_model=DocumentResponse)
async def update_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    title_utf8: Optional[str] = None,
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """
    更新文档（仅管理员，先删旧数据再重新解析入库）

    流程：
    1. 删除旧文档的向量数据 + 图片
    2. 用新文件内容重新解析、分块、向量化
    3. 更新 DB 记录（保留原 document_id 和可见性）
    """
    try:
        doc = await db.get_document(document_id)

        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档不存在"
            )

        # 读取新文件
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="文件内容为空")

        raw_filename = file.filename or doc.get("filename", "unnamed")
        filename = _fix_encoding(raw_filename)
        if title_utf8:
            title = title_utf8
        elif title:
            title = _fix_encoding(title)
        else:
            title = doc.get("title", filename)

        # 1+2. 删除旧向量数据 + 旧图片（业务下沉 document_service）
        await purge_document_artifacts(document_id, updated=True)

        # 3. 更新 DB 记录状态（递增版本号，追踪文档版本）
        file_hash = _compute_file_hash(content)
        old_version = doc.get("version", 1)
        await db.update_document(document_id, {
            "status": DocumentStatus.PROCESSING.value,
            "filename": filename,
            "title": title,
            "chunk_count": 0,
            "char_count": 0,
            "file_hash": file_hash,
            "version": old_version + 1,
        })

        # 4. 后台异步重新处理（保持原文档可见性：user_id 沿用原文档记录）
        # 注意：update 始终走 BackgroundTasks，不经 USE_CELERY 分支（与拆分前行为一致）
        uploader = get_document_uploader()
        background_tasks.add_task(
            _process_document,
            db, uploader, document_id, content, filename, title, doc.get("user_id", ""),
            doc.get("extra_metadata"),
        )

        logger.info(f"文档更新中: {document_id} - {filename}")

        return DocumentResponse(
            document_id=document_id,
            filename=filename,
            title=title,
            status=DocumentStatus.PROCESSING.value,
            chunk_count=0,
            char_count=0,
            created_at=str(doc.get("created_at", "")),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文档更新失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"文档更新失败: {str(e)}"
        )


# ========== 批量入库 ==========

@router.post("/batch-upload", response_model=BatchUploadResponse)
async def batch_upload_documents(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    skip_duplicate: bool = Form(True),
    shared_to_diagnosis: bool = Form(True),  # 共享给自动诊断（旁路组织隔离，敏感文档可关闭）
    current_user: UserResponse = Depends(require_admin),
    db: Database = Depends(get_db),
):
    """
    批量上传文档（仅管理员）

    一次上传多个文件，自动去重（skip_duplicate=True 时）。
    文件处理在后台异步执行，接口立即返回。
    管理员导入的文档默认为公共文档。

    返回每个文件的处理结果：
    - success: 已提交处理
    - skipped: 重复文件跳过
    - failed: 文件类型不支持或读取失败
    """
    try:
        admin_user_id = current_user.user_id
        # 注意：batch 保留内联白名单（与单文件上传的 _ALLOWED_EXTENSIONS 是两处独立校验，
        # 历史行为如此，T10 拆分不合并）
        supported_types = {"pdf", "docx", "txt", "md", "markdown"}

        results: List[dict] = []
        success_count = 0
        skipped_count = 0
        failed_count = 0

        for file in files:
            raw_filename = file.filename or "unnamed"
            filename = _fix_encoding(raw_filename)
            doc_type = filename.split(".")[-1].lower() if "." in filename else ""

            # 文件类型检查
            if doc_type not in supported_types:
                failed_count += 1
                results.append({
                    "filename": filename,
                    "status": "failed",
                    "reason": f"不支持的文件类型: {doc_type}",
                })
                continue

            # 读取内容
            content = await file.read()
            if not content:
                failed_count += 1
                results.append({
                    "filename": filename,
                    "status": "failed",
                    "reason": "文件内容为空",
                })
                continue

            # 去重检查（公共文档全局去重）
            file_hash = _compute_file_hash(content)
            if skip_duplicate:
                existing = await _check_duplicate(db, "", file_hash)
                if existing:
                    skipped_count += 1
                    results.append({
                        "filename": filename,
                        "status": "skipped",
                        "document_id": existing.get("document_id", ""),
                        "reason": "文件已存在",
                    })
                    continue

            # 解析 Markdown YAML frontmatter → 运维业务 metadata（与单文件上传一致）
            biz_meta, doc_type = extract_biz_meta(content, shared_to_diagnosis, doc_type)

            # 创建文档记录（公共文档）
            document_id, document = build_document_record(
                admin_user_id=admin_user_id,
                filename=filename,
                title=filename,
                doc_type=doc_type,
                file_hash=file_hash,
                biz_meta=biz_meta,
            )
            await db.create_document(document)

            # 异步处理：USE_CELERY 走 Celery worker；否则降级 BackgroundTasks
            await submit_document_processing(
                db, background_tasks,
                document_id=document_id, content=content, filename=filename,
                title=filename, admin_user_id=admin_user_id, biz_meta=biz_meta,
            )

            success_count += 1
            results.append({
                "filename": filename,
                "status": "pending",
                "document_id": document_id,
            })

        logger.info(
            f"批量上传完成: {len(files)} 个文件, "
            f"成功 {success_count}, 跳过 {skipped_count}, 失败 {failed_count}"
        )

        return BatchUploadResponse(
            total=len(files),
            success=success_count,
            skipped=skipped_count,
            failed=failed_count,
            results=results,
        )

    except Exception as e:
        logger.exception(f"批量上传失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="批量上传失败，请稍后重试"
        )


# ========== 多模态 RAG：图片访问 ==========

# 图片接口安全校验：document_id 格式为 doc_ + 12位hex（见上传处 uuid.uuid4().hex[:12]）
_DOC_ID_PATTERN = re.compile(r"^doc_[a-f0-9]{12}$")
# 图片文件名格式：img_ + 12位hex + 扩展名
_IMG_NAME_PATTERN = re.compile(r"^img_[a-f0-9]{12}\.(png|jpe?g|webp)$", re.IGNORECASE)
_IMG_MIME_MAP = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


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
    # 安全校验 1：白名单校验 document_id（防止 ../../etc/passwd 注入）
    if not _DOC_ID_PATTERN.match(document_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文档 ID"
        )

    # 安全校验 2：白名单校验 image_name（防止路径穿越、扩展名伪造）
    if not _IMG_NAME_PATTERN.match(image_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的图片名"
        )

    from fastapi.responses import Response

    from ..document.image_store import get_image_store

    relative_path = f"{document_id}/{image_name}"

    # 安全校验 3：二次校验最终路径不逃逸根目录（纵深防御）
    image_store = get_image_store()
    target_path = (image_store.root_dir / relative_path).resolve()
    if not str(target_path).startswith(str(image_store.root_dir) + os.sep):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的图片路径"
        )

    image_bytes = image_store.read_bytes(relative_path)
    if image_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="图片不存在"
        )

    # 推断 MIME（已通过正则白名单校验扩展名，安全）
    ext = image_name.rsplit(".", 1)[-1].lower()
    media_type = _IMG_MIME_MAP.get(ext, "image/png")

    return Response(content=image_bytes, media_type=media_type)
