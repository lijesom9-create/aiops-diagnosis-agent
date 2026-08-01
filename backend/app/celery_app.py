"""
Celery 应用实例

用于异步处理文档导入（解析/分块/向量化）等耗时任务。
broker 和 result backend 复用 Redis。

启动 worker（在 backend/ 目录）：
    celery -A app.celery_app worker --loglevel=info --pool=solo

Windows 注意：推荐 --pool=solo（prefork 在 Windows 下有兼容问题）。
"""

import sys
from pathlib import Path

# 确保 backend/ 在 sys.path（celery 命令行启动时需要）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from celery import Celery
from app.core.config import settings

# broker / backend 优先用 CELERY_* 配置，留空则复用 REDIS_URL
broker_url = settings.CELERY_BROKER_URL or settings.REDIS_URL or "redis://localhost:6379/0"
result_backend = settings.CELERY_RESULT_BACKEND or settings.REDIS_URL or "redis://localhost:6379/0"

app = Celery(
    "education_agent",
    broker=broker_url,
    backend=result_backend,
    include=["app.tasks.document_tasks"],
)

app.conf.update(
    # 序列化（json 安全，pickle 有风险）
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 任务超时（大文档解析可能较慢）
    task_soft_time_limit=600,   # 软超时 10 分钟
    task_time_limit=900,        # 硬超时 15 分钟
    # 可靠性：worker 崩溃时任务重新分配；长任务一次只取一个避免饥饿
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
