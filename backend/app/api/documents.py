"""
Document API - 文档管理

支持 PDF/Word/TXT/Markdown 上传，解析、分块、向量化。
包含：去重检查、批量入库、文档更新（先删后加）。
"""

import hashlib
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile, status
from loguru import logger
from pydantic import BaseModel

from ..core.auth import UserResponse, get_current_user, require_admin
from ..core.config import settings
from ..core.database import Database, get_db
from ..document.uploader import DocumentUploader
from ..models.document import DocumentCategory, DocumentStatus
from ..storage.file_storage import get_file_storage

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


# 允许的文件扩展名白名单
_ALLOWED_EXTENSIONS = {"pdf", "docx", "txt", "md", "markdown"}


def _sanitize_filename(filename: str) -> str:
    """清理文件名，防止路径穿越和特殊字符注入

    - 只取 basename（去除任何路径分隔符）
    - 去除控制字符
    - 限制长度（128 字节）
    """
    if not filename:
        return "unnamed"
    # 只取 basename，防止 ../../etc/passwd 这类注入
    safe = Path(filename).name
    # 去除控制字符（\x00-\x1f, \x7f）
    safe = re.sub(r"[\x00-\x1f\x7f]", "_", safe)
    # 限制长度
    if len(safe.encode("utf-8")) > 128:
        # 保留扩展名，截断主名
        stem = Path(safe).stem[:60]
        suffix = Path(safe).suffix
        safe = f"{stem}{suffix}"
    return safe or "unnamed"


def _extract_extension(filename: str) -> str:
    """安全提取文件扩展名（小写，无点）"""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


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
    category: str = "other"
    tags: List[str] = []
    version: int = 1


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


def _compute_file_hash(content: bytes) -> str:
    """计算文件内容的 MD5 哈希（用于去重）"""
    return hashlib.md5(content).hexdigest()


async def _check_duplicate(
    db: Database, user_id: str, file_hash: str
) -> Optional[dict]:
    """检查用户是否已上传过相同哈希的文档

    Returns:
        已存在的文档记录（dict），如不存在返回 None
    """
    documents = await db.get_user_documents(user_id)
    for doc in documents:
        if doc.get("file_hash") == file_hash:
            return doc
    return None


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
        valid_categories = [c.value for c in DocumentCategory]
        if category not in valid_categories:
            category = "other"

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
        # 与 scripts/seed_ops_kb.py 共用逻辑；无 frontmatter 时为空 dict，不影响原有流程
        from app.document.frontmatter import extract_business_metadata, parse_frontmatter
        biz_meta: dict = {}
        try:
            content_text = content.decode("utf-8", errors="ignore")
            frontmatter, _ = parse_frontmatter(content_text)
            biz_meta = extract_business_metadata(frontmatter)
        except Exception as e:
            logger.debug(f"frontmatter 解析失败（忽略，按普通文档处理）: {e}")
        # 诊断共享标记随 chunk metadata 入库（字符串形式，检索过滤器按 "true" 匹配）；
        # 上传端点仅 admin 可调，默认共享符合"运维知识供诊断系统使用"的预期，敏感文档显式关闭
        biz_meta["shared_to_diagnosis"] = "true" if shared_to_diagnosis else "false"

        # frontmatter 声明的 doc_type 优先于文件扩展名（如 manual/incident/sop 业务分类）
        if biz_meta.get("doc_type"):
            doc_type = str(biz_meta["doc_type"]).lower()

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
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat()
        document = {
            "document_id": document_id,
            "user_id": "",  # 公共文档：留空使检索层对所有用户可见
            "uploaded_by": admin_user_id,  # 审计：记录导入者
            "is_public": True,  # 列表层据此返回给所有用户
            "filename": filename,
            "title": title or filename,
            "doc_type": doc_type,
            "status": DocumentStatus.PENDING.value,
            "chunk_count": 0,
            "char_count": 0,
            "file_hash": file_hash,
            "created_at": now,
            "category": category,
            "tags": [],
            # 运维业务 metadata（frontmatter 解析）：供文档列表展示 & 检索过滤
            "service": biz_meta.get("service"),
            "severity": biz_meta.get("severity"),
            "incident_id": biz_meta.get("incident_id"),
            "extra_metadata": biz_meta or None,
        }

        await db.create_document(document)

        # 异步处理：USE_CELERY=True 走 Celery worker；False 降级到 BackgroundTasks 同步处理
        if settings.USE_CELERY:
            # Celery 模式：先存文件到 FileStorage，再投递 task（task 从路径读取，避免 bytes 过 broker）
            file_storage = get_file_storage()
            file_info = await file_storage.save(
                content=content, filename=filename,
                user_id=admin_user_id, document_id=document_id,
            )
            file_path = file_info["file_path"]
            await db.update_document(document_id, {"file_path": file_path})

            from app.tasks.document_tasks import process_document
            task = process_document.delay(
                document_id, file_path, filename, title or filename, biz_meta,
            )
            await db.update_document(document_id, {"task_id": task.id})
            logger.info(f"文档已投递 Celery task: {document_id} task={task.id} (导入者: {admin_user_id})")
        else:
            # 降级模式：BackgroundTasks 同步处理（user_id 传空=公共文档）
            uploader = get_document_uploader()
            background_tasks.add_task(
                _process_document,
                db, uploader, document_id, content, filename, title, "", biz_meta,
            )
            logger.info(f"文档已提交 BackgroundTasks: {document_id} - {filename} (导入者: {admin_user_id})")

        return DocumentResponse(
            document_id=document_id,
            filename=filename,
            title=title or filename,
            status=DocumentStatus.PENDING.value,
            chunk_count=0,
            char_count=0,
            created_at=now,
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

        # 1. 删除旧向量数据
        if _knowledge_store:
            try:
                _knowledge_store.delete_by_document(document_id)
                logger.info(f"更新文档：已删除旧向量数据 {document_id}")
            except Exception as e:
                logger.warning(f"删除旧向量数据失败: {e}")

        # 2. 删除旧图片
        try:
            from ..document.image_store import get_image_store
            get_image_store().delete_document_images(document_id)
        except Exception as e:
            logger.warning(f"删除旧图片失败: {e}")

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

        # 4. 后台异步重新处理（保持原文档可见性：公共文档 user_id 为空）
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

class BatchUploadResponse(BaseModel):
    """批量上传响应"""
    total: int
    success: int
    skipped: int
    failed: int
    results: List[dict]


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
            from app.document.frontmatter import extract_business_metadata, parse_frontmatter
            biz_meta: dict = {}
            try:
                frontmatter, _ = parse_frontmatter(content.decode("utf-8", errors="ignore"))
                biz_meta = extract_business_metadata(frontmatter)
            except Exception as e:
                logger.debug(f"frontmatter 解析失败（忽略，按普通文档处理）: {e}")
            # 诊断共享标记随 chunk metadata 入库（与单文件上传一致）
            biz_meta["shared_to_diagnosis"] = "true" if shared_to_diagnosis else "false"
            if biz_meta.get("doc_type"):
                doc_type = str(biz_meta["doc_type"]).lower()

            # 创建文档记录（公共文档）
            document_id = f"doc_{uuid.uuid4().hex[:12]}"
            now = datetime.now().isoformat()
            document = {
                "document_id": document_id,
                "user_id": "",  # 公共文档
                "uploaded_by": admin_user_id,
                "is_public": True,
                "filename": filename,
                "title": filename,
                "doc_type": doc_type,
                "status": DocumentStatus.PENDING.value,
                "chunk_count": 0,
                "char_count": 0,
                "file_hash": file_hash,
                "created_at": now,
                "service": biz_meta.get("service"),
                "severity": biz_meta.get("severity"),
                "incident_id": biz_meta.get("incident_id"),
                "extra_metadata": biz_meta or None,
            }
            await db.create_document(document)

            # 异步处理：USE_CELERY 走 Celery worker；否则降级 BackgroundTasks
            if settings.USE_CELERY:
                file_storage = get_file_storage()
                file_info = await file_storage.save(
                    content=content, filename=filename,
                    user_id=admin_user_id, document_id=document_id,
                )
                file_path = file_info["file_path"]
                await db.update_document(document_id, {"file_path": file_path})

                from app.tasks.document_tasks import process_document
                task = process_document.delay(document_id, file_path, filename, filename, biz_meta)
                await db.update_document(document_id, {"task_id": task.id})
            else:
                uploader = get_document_uploader()
                background_tasks.add_task(
                    _process_document,
                    db, uploader, document_id, content, filename, filename, "", biz_meta,
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


# ========== 后台处理 ==========

async def _process_document(
    db: Database,
    uploader: DocumentUploader,
    document_id: str,
    content: bytes,
    filename: str,
    title: Optional[str],
    user_id: str,
    extra_metadata: Optional[dict] = None,
):
    """后台处理文档：解析、分块、向量化

    extra_metadata: 运维业务 metadata（frontmatter 解析的 doc_type/service/severity 等），
    注入到每个 chunk，支撑检索层 metadata_filter 精准过滤。
    """
    try:
        await db.update_document(document_id, {"status": DocumentStatus.PROCESSING.value})

        result = await uploader.upload(
            content=content,
            filename=filename,
            title=title,
            user_id=user_id,
            document_id=document_id,
            extra_metadata=extra_metadata,
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
