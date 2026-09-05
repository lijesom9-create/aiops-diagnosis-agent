"""文档上传/处理业务服务（T10 从 api/documents.py 下沉）。

承接文档摄取域的全部业务编排：
- 文件名/编码安全处理（_fix_encoding / _sanitize_filename / _extract_extension）
- 分类校验（normalize_category）与哈希去重（_compute_file_hash / _check_duplicate）
- frontmatter 业务 metadata 解析（extract_biz_meta，upload 与 batch-upload 消重）
- 公共文档记录构造（build_document_record，消重；category/tags 仅单文件上传写入）
- 处理投递（submit_document_processing：USE_CELERY / BackgroundTasks 双分支，消重）
- 产物清理（purge_document_artifacts：删向量 + 删图片，delete/update 消重）
- 后台解析流水线（_process_document：解析、分块、向量化）
- 上传单例（get_document_uploader）与知识库注入（set_knowledge_store）

api/documents.py 保留薄 handler + 符号转发（main.py / 测试的导入路径不变）。
"""

import asyncio
import hashlib
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import BackgroundTasks
from loguru import logger
from pydantic import BaseModel

from ..core.config import settings
from ..core.database import Database
from ..document.uploader import DocumentUploader
from ..models.document import DocumentCategory, DocumentStatus
from ..storage.file_storage import get_file_storage

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


class BatchUploadResponse(BaseModel):
    """批量上传响应"""
    total: int
    success: int
    skipped: int
    failed: int
    results: List[dict]


# ========== 文件名/编码安全处理 ==========

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


def normalize_category(category: str) -> str:
    """校验文档分类，非法值回退为 other（upload 端点业务规则）"""
    valid_categories = [c.value for c in DocumentCategory]
    if category not in valid_categories:
        return "other"
    return category


# ========== 上传单例与知识库注入 ==========

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


# ========== 去重与记录构造 ==========

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


def extract_biz_meta(content: bytes, shared_to_diagnosis: bool, doc_type: str) -> tuple:
    """解析 Markdown YAML frontmatter → 运维业务 metadata，并按声明覆盖 doc_type。

    upload 与 batch-upload 消重共用。无 frontmatter 或解析失败时 biz_meta 为空
    dict，按普通文档处理，不影响原流程。

    诊断共享标记随 chunk metadata 入库（字符串形式，检索过滤器按 "true" 匹配）；
    上传端点仅 admin 可调，默认共享符合"运维知识供诊断系统使用"的预期，敏感文档显式关闭。
    frontmatter 声明的 doc_type 优先于文件扩展名（如 manual/incident/sop 业务分类）。

    Returns:
        (biz_meta, doc_type)
    """
    from app.document.frontmatter import extract_business_metadata, parse_frontmatter
    biz_meta: dict = {}
    try:
        frontmatter, _ = parse_frontmatter(content.decode("utf-8", errors="ignore"))
        biz_meta = extract_business_metadata(frontmatter)
    except Exception as e:
        logger.debug(f"frontmatter 解析失败（忽略，按普通文档处理）: {e}")
    biz_meta["shared_to_diagnosis"] = "true" if shared_to_diagnosis else "false"
    if biz_meta.get("doc_type"):
        doc_type = str(biz_meta["doc_type"]).lower()
    return biz_meta, doc_type


def build_document_record(
    *,
    admin_user_id: str,
    filename: str,
    title: str,
    doc_type: str,
    file_hash: str,
    biz_meta: dict,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> tuple:
    """构造公共文档记录并生成 document_id（upload 与 batch-upload 消重）。

    公共文档语义：user_id 留空使检索层对所有用户可见，uploaded_by 记录导入者便于审计。
    category/tags 仅单文件上传端点写入（batch 历史行为不写这两个键，传默认 None 保持
    存储键集不变，列表层对缺失 category 回退 "other"）。

    Returns:
        (document_id, document)
    """
    document_id = f"doc_{uuid.uuid4().hex[:12]}"
    now = datetime.now().isoformat()
    document = {
        "document_id": document_id,
        "user_id": "",  # 公共文档：留空使检索层对所有用户可见
        "uploaded_by": admin_user_id,  # 审计：记录导入者
        "is_public": True,  # 列表层据此返回给所有用户
        "filename": filename,
        "title": title,
        "doc_type": doc_type,
        "status": DocumentStatus.PENDING.value,
        "chunk_count": 0,
        "char_count": 0,
        "file_hash": file_hash,
        "created_at": now,
        # 运维业务 metadata（frontmatter 解析）：供文档列表展示 & 检索过滤
        "service": biz_meta.get("service"),
        "severity": biz_meta.get("severity"),
        "incident_id": biz_meta.get("incident_id"),
        "extra_metadata": biz_meta or None,
    }
    if category is not None:
        document["category"] = category
        document["tags"] = tags or []
    return document_id, document


# ========== 处理投递与产物清理 ==========

async def submit_document_processing(
    db: Database,
    background_tasks: BackgroundTasks,
    *,
    document_id: str,
    content: bytes,
    filename: str,
    title: str,
    admin_user_id: str,
    biz_meta: dict,
    audit_log: bool = False,
) -> None:
    """投递文档异步处理（upload 与 batch-upload 消重）。

    USE_CELERY=True 走 Celery worker（先存文件到 FileStorage，task 从路径读取，
    避免 bytes 过 broker）；False 降级到 BackgroundTasks 同步处理（user_id 传空=公共文档）。
    Celery 分支的 title 落 `title or filename`，BackgroundTasks 分支保留原样传 title
    （与拆分前逐字一致：单文件上传两分支本就不对称，batch 传 filename 无差异）。
    audit_log=True（单文件上传）时记录含导入者的审计日志；batch 保持原有静默。

    幂等补偿：投递链路（存文件 / 写 file_path / task.delay）任何一步失败时，
    先将记录标记 FAILED（retry 端点仅接受 failed/completed）再上抛，
    避免记录永久卡在 PENDING 成为"幽灵文档"。
    """
    try:
        if settings.USE_CELERY:
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
            if audit_log:
                logger.info(f"文档已投递 Celery task: {document_id} task={task.id} (导入者: {admin_user_id})")
        else:
            uploader = get_document_uploader()
            background_tasks.add_task(
                _process_document,
                db, uploader, document_id, content, filename, title, "", biz_meta,
            )
            if audit_log:
                logger.info(f"文档已提交 BackgroundTasks: {document_id} - {filename} (导入者: {admin_user_id})")
    except Exception as e:
        try:
            await db.update_document(document_id, {
                "status": DocumentStatus.FAILED.value,
                "error_message": f"处理任务投递失败: {str(e)[:200]}",
            })
        except Exception:
            # 补偿写失败仅记日志，不掩盖原始投递异常
            logger.warning(f"投递失败补偿标记写入失败: {document_id}")
        raise


async def purge_document_artifacts(document_id: str, *, updated: bool = False) -> Tuple[bool, bool]:
    """删除文档的向量数据与图片（delete / update 消重）。

    updated=True 为 update 流程：成功删向量记 info 日志，失败日志措辞带"旧"。

    Returns:
        (vector_ok, images_ok)——失败不抛异常，由调用方决定补偿策略
        （delete 流程挂起记录待 sweep 重试；update 流程终止重处理防重复 chunk）。
    """
    vector_ok = True
    if _knowledge_store:
        try:
            _knowledge_store.delete_by_document(document_id)
            if updated:
                logger.info(f"更新文档：已删除旧向量数据 {document_id}")
        except Exception as e:
            vector_ok = False
            logger.warning(f"删除{'旧' if updated else ''}向量数据失败: {e}")

    images_ok = True
    try:
        from ..document.image_store import get_image_store
        get_image_store().delete_document_images(document_id)
    except Exception as e:
        images_ok = False
        logger.warning(f"删除{'旧' if updated else '文档'}图片失败: {e}")

    return vector_ok, images_ok


async def delete_document_with_cleanup(db: Database, document_id: str) -> str:
    """删除文档（带孤儿向量防护）。

    向量清理失败时**不删记录**：标记 status=deleting + purge_pending 挂起，
    由 document_stale_sweep_loop 周期重试清理，成功后才移除记录——
    避免"记录已删、向量仍在"的孤儿数据继续被检索命中。

    Returns:
        "deleted" | "deferred" | "not_found"
    """
    doc = await db.get_document(document_id)
    if not doc:
        return "not_found"

    vector_ok, _ = await purge_document_artifacts(document_id)
    if not vector_ok:
        await db.update_document(document_id, {
            "status": DocumentStatus.DELETING.value,
            "purge_pending": True,
            "error_message": "向量数据清理失败，删除挂起（系统将自动重试）",
        })
        logger.warning(f"文档删除挂起（向量清理失败，待 sweep 重试）: {document_id}")
        return "deferred"

    await db.delete_document(document_id)
    logger.info(f"文档删除成功: {document_id}")
    return "deleted"


async def process_batch_file(
    db: Database,
    background_tasks: BackgroundTasks,
    *,
    file,
    admin_user_id: str,
    skip_duplicate: bool,
    shared_to_diagnosis: bool,
) -> dict:
    """处理批量上传中的单个文件：校验 → 去重 → 建档 → 投递。

    校验类失败（类型不支持/内容为空）返回 failed 条目而不抛异常——调用方逐文件
    计数继续处理下一文件（原实现单文件异常会中断整批）；
    建档后的投递异常向上传播（submit_document_processing 内已补偿标记 FAILED）。

    注意：batch 保留内联白名单（与单文件上传的 _ALLOWED_EXTENSIONS 是两处独立
    校验，T10 拆分时按"保留历史差异"原则未合并）。

    Returns:
        结果条目，status ∈ {pending, skipped, failed}
    """
    raw_filename = file.filename or "unnamed"
    filename = _fix_encoding(raw_filename)
    doc_type = filename.split(".")[-1].lower() if "." in filename else ""

    supported_types = {"pdf", "docx", "txt", "md", "markdown"}
    if doc_type not in supported_types:
        return {
            "filename": filename,
            "status": "failed",
            "reason": f"不支持的文件类型: {doc_type}",
        }

    content = await file.read()
    if not content:
        return {
            "filename": filename,
            "status": "failed",
            "reason": "文件内容为空",
        }

    # 去重检查（公共文档全局去重）
    file_hash = _compute_file_hash(content)
    if skip_duplicate:
        existing = await _check_duplicate(db, "", file_hash)
        if existing:
            return {
                "filename": filename,
                "status": "skipped",
                "document_id": existing.get("document_id", ""),
                "reason": "文件已存在",
            }

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

    return {
        "filename": filename,
        "status": "pending",
        "document_id": document_id,
    }


# ========== 卡死恢复（幂等补偿扫描，复用事故域 stale sweep 先例） ==========

async def recover_stale_documents() -> int:
    """启动/周期捞回：卡死的 PENDING/PROCESSING → FAILED（可经 retry 端点重投）。

    卡死来源：投递前进程重启（BackgroundTask 丢失）、worker 崩溃（Celery 无
    acks_late，task 静默丢失）、Mongo 写挂等。误伤窗口：真实处理超
    DOC_STALE_SECONDS 的大文档会被提前标失败，但 worker 后续成功会覆盖回
    COMPLETED（最终状态一致），阈值默认 1800s 足够宽裕。
    """
    from ..core.database import db as _db
    stale = await _db.find_stale_documents(settings.DOC_STALE_SECONDS)
    for doc in stale:
        await _db.update_document(doc.get("document_id"), {
            "status": DocumentStatus.FAILED.value,
            "error_message": f"处理超时（>{settings.DOC_STALE_SECONDS}s 未推进），已自动标记失败，可重试",
        })
        logger.warning(
            f"卡死文档已标记失败: {doc.get('document_id')} 原状态={doc.get('status')}"
        )
    return len(stale)


async def finalize_pending_deletes() -> int:
    """finalize deleting 挂起文档：重试向量清理，成功后移除记录。"""
    from ..core.database import db as _db
    pending = await _db.find_documents_by_status(DocumentStatus.DELETING.value)
    done = 0
    for doc in pending:
        doc_id = doc.get("document_id")
        vector_ok, _ = await purge_document_artifacts(doc_id)
        if vector_ok:
            await _db.delete_document(doc_id)
            done += 1
            logger.info(f"挂起删除完成（向量清理成功）: {doc_id}")
    return done


async def document_stale_sweep_loop() -> None:
    """文档卡死扫描循环（main.py lifespan 启动，独立于告警诊断域）"""
    interval = max(60, settings.DOC_SWEEP_INTERVAL_SECONDS)
    while True:
        try:
            await recover_stale_documents()
            await finalize_pending_deletes()
        except Exception as e:
            logger.warning(f"文档卡死扫描失败（下轮重试）: {e}")
        await asyncio.sleep(interval)


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
