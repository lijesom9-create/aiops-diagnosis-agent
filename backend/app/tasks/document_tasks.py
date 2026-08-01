"""
文档处理异步任务（Celery）

把文档解析/分块/向量化封装为 Celery task，避免阻塞 web 请求。
worker 进程独立于 FastAPI，需自行初始化 knowledge_store。

事件循环策略：worker 用持久事件循环（_run_async），所有 task 共享一个循环，
避免 motor（异步 MongoDB 驱动）跨事件循环的 "Future attached to a different loop" 问题。
"""

import asyncio
from datetime import datetime
from pathlib import Path
from loguru import logger

from app.celery_app import app
from app.shared_services import init_knowledge_store, get_knowledge_store
from app.core.database import db
from app.document.uploader import DocumentUploader

# 持久事件循环（solo pool 单线程，所有 task 共享，避免 motor 跨循环）
_loop = None


def _run_async(coro):
    """在持久事件循环里跑协程"""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


@app.task(
    bind=True,
    name="app.tasks.document_tasks.process_document",
    autoretry_for=(Exception,),
    retry_backoff=True,       # 指数退避：1s, 2s, 4s...
    retry_backoff_max=300,    # 最大退避 5 分钟
    retry_jitter=True,        # 抖动避免任务雪崩
    max_retries=3,
)
def process_document(self, document_id: str, file_path: str, filename: str, title: str = ""):
    """异步处理文档：解析 → 分块 → 向量化 → 入库

    Args:
        document_id: 文档 ID
        file_path: web 端已存储的文件绝对路径
        filename: 原始文件名
        title: 文档标题

    user_id 传 None 给 uploader：
      - 跳过 uploader 内部 file_storage.save（文件已由 web 端存好）
      - metadata.user_id 为空 → 检索层判定为公共文档（所有用户可问答）
    """
    logger.info(f"[task] 开始处理文档: {document_id} - {filename} (第 {self.request.retries + 1} 次尝试)")

    async def _run():
        # 1. 连接 DB + 更新状态为 processing
        await db.connect()
        await db.update_document(document_id, {
            "status": "processing",
            "started_at": datetime.now().isoformat(),
        })

        # 2. 初始化 knowledge_store（worker 独立进程，不跑 FastAPI lifespan）
        store = get_knowledge_store()
        if store is None:
            logger.info("[task] knowledge_store 未初始化，开始初始化...")
            store = init_knowledge_store()

        # 3. 读取文件内容（web 端已存储到 file_path）
        content = Path(file_path).read_bytes()
        if not content:
            raise ValueError(f"文件内容为空: {file_path}")

        # 4. 上传入库
        uploader = DocumentUploader(knowledge_store=store)
        result = await uploader.upload(
            content=content,
            filename=filename,
            title=title or filename,
            user_id=None,
            document_id=document_id,
        )

        # 5. 更新状态为 completed
        await db.update_document(document_id, {
            "status": "completed",
            "chunk_count": result.get("chunk_count", 0),
            "char_count": result.get("char_count", 0),
            "finished_at": datetime.now().isoformat(),
        })
        return result

    try:
        result = _run_async(_run())
        logger.info(f"[task] 文档处理完成: {document_id}, {result.get('chunk_count')} 块")
        return {
            "document_id": document_id,
            "status": "completed",
            "chunk_count": result.get("chunk_count", 0),
        }
    except Exception as e:
        logger.exception(f"[task] 文档处理失败: {document_id} - {e}")
        # 仅在最后一次重试失败时标记 failed；重试中保持 processing（用户看到"处理中"）
        if self.request.retries >= (self.max_retries or 0):
            _run_async(_mark_failed(document_id, f"{type(e).__name__}: {e}"))
        raise  # 触发 Celery 自动重试


async def _mark_failed(document_id: str, error: str):
    """标记文档为失败状态"""
    await db.connect()
    await db.update_document(document_id, {
        "status": "failed",
        "error_message": error,
        "finished_at": datetime.now().isoformat(),
    })
