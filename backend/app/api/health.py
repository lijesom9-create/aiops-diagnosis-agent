"""
Health & Metrics API - 健康检查与运行时指标

提供：
- /health: 服务健康状态
- /metrics: 运行时指标（检索延迟、调用次数等）
"""

import time
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from loguru import logger

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
    """轻量存活检查（用于 Docker HEALTHCHECK，不查询向量库）"""
    return {"status": "ok"}


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
