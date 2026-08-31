"""
Health & Metrics API - 健康检查与运行时指标

提供：
- /health: 服务健康状态
- /metrics: 运行时指标（检索延迟、调用次数等）
"""

import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status
from loguru import logger
from pydantic import BaseModel

from ..observability.metrics import get_metrics

router = APIRouter(prefix="/api/health", tags=["健康检查"])

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


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    timestamp: float
    version: str = "1.0.0"
    checks: Dict[str, Any]


@router.get("/live")
async def liveness_check():
    """存活检查（liveness）：进程活着即 ok，不查依赖——依赖故障应触发
    readiness 失败而非重启容器（重启解决不了 Mongo 挂了）"""
    return {"status": "ok"}


@router.get("/ready")
async def readiness_check():
    """就绪检查（readiness）：真实探测依赖——Mongo / Redis / Qdrant 知识库

    Docker HEALTHCHECK 与编排依赖使用本端点：/live 恒真导致"容器永远 healthy、
    依赖全挂也照常"的空壳问题由本端点修正。
    """
    checks: Dict[str, Any] = {}
    ready = True

    # MongoDB
    try:
        from ..core.database import db
        await db.connect()
        await db._mongo.command("ping")
        checks["mongodb"] = {"status": "ok"}
    except Exception as e:
        ready = False
        checks["mongodb"] = {"status": "error", "message": str(e)[:120]}

    # Redis（可选项：未配置/降级内存不视为不就绪）
    try:
        from ..core.cache import get_cache
        cache = get_cache()
        redis_client = getattr(cache, "_redis", None)
        if redis_client is not None:
            redis_client.ping()
            checks["redis"] = {"status": "ok"}
        else:
            checks["redis"] = {"status": "disabled", "mode": "memory_fallback"}
    except Exception as e:
        # Redis 配置了但挂了：降级内存缓存仍可服务，标记降级而非不就绪
        checks["redis"] = {"status": "degraded", "message": str(e)[:120]}

    # Qdrant 知识库
    try:
        store = _get_store()
        checks["knowledge_store"] = {"status": "ok", "records": store.vector_store.size()}
    except Exception as e:
        ready = False
        checks["knowledge_store"] = {"status": "error", "message": str(e)[:120]}

    if not ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "checks": checks},
        )

    return {"status": "ready", "timestamp": time.time(), "checks": checks}


@router.get("", response_model=HealthResponse)
async def health_check():
    """
    服务健康检查

    检查项：
    - knowledge_store: 向量存储是否可用
    - metrics: 指标收集器是否可用
    """
    checks: Dict[str, Any] = {}
    healthy = True

    # 检查知识库
    try:
        store = _get_store()
        child_size = store.vector_store.size()
        checks["knowledge_store"] = {
            "status": "ok",
            "child_records": child_size,
            "parent_records": store._parent_store.size() if store._separate_parent_child else 0,
        }
    except Exception as e:
        healthy = False
        checks["knowledge_store"] = {"status": "error", "message": str(e)}

    # 检查指标收集器
    try:
        metrics = get_metrics()
        checks["metrics"] = {"status": "ok", "metric_count": len(metrics.get_all_metrics())}
    except Exception as e:
        healthy = False
        checks["metrics"] = {"status": "error", "message": str(e)}

    if not healthy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unhealthy", "checks": checks},
        )

    return HealthResponse(
        status="healthy",
        timestamp=time.time(),
        checks=checks,
    )


@router.get("/metrics")
async def get_runtime_metrics():
    """
    获取运行时指标

    返回检索调用次数、延迟分布等关键指标。
    """
    try:
        metrics = get_metrics()
        return {
            "metrics": metrics.get_all_metrics(),
            "recent_history": metrics.get_history(limit=50),
        }
    except Exception as e:
        logger.error(f"获取指标失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取指标失败: {str(e)}"
        )
