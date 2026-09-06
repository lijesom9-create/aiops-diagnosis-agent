"""
个人知识助手 - 主应用入口
基于 RAG + LangGraph 的智能问答系统
"""

# 必须在所有 import 之前设置，否则 sentence-transformers 仍会联网检查更新
import os

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

import asyncio
import re
import sys
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import admin, alerts, auth, documents, health, incidents, knowledge, langgraph, memory, monitoring
from app.core.config import settings
from app.core.database import db

# ========== 结构化日志配置 ==========
# request_id 贯穿请求生命周期，便于生产环境追踪完整调用链
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

# patcher：每条日志自动从 contextvar 读取 request_id 注入到 extra
def _request_id_patcher(record):
    record["extra"]["request_id"] = request_id_ctx.get()

# 配置日志格式：时间 | 级别 | request_id | 模块:行号 | 消息
logger.remove()
logger.configure(extra={"request_id": "-"}, patcher=_request_id_patcher)
_log_format = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[request_id]}</cyan> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
)
logger.add(
    sys.stderr,
    format=_log_format,
    level=os.environ.get("LOG_LEVEL", "INFO"),
    colorize=True,
    backtrace=settings.is_development,
    diagnose=settings.is_development,  # 生产环境不泄露变量值
)


# 应用生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""

    # 启动时
    logger.info(f"启动 {settings.APP_NAME} v{settings.APP_VERSION}")

    # 连接数据库
    await db.connect()

    # 初始化统一知识存储（复用 shared_services.init_knowledge_store，celery worker 也用同一函数）
    from app.api.documents import set_knowledge_store
    from app.api.health import set_knowledge_store as set_health_store
    from app.api.knowledge import set_knowledge_store as set_kb_store
    from app.shared_services import init_knowledge_store

    knowledge_store = init_knowledge_store()
    # 同步到各 API 模块的本地引用
    set_knowledge_store(knowledge_store)
    set_kb_store(knowledge_store)
    set_health_store(knowledge_store)

    logger.info(f"应用启动完成, 知识库: {knowledge_store.size()} 条")

    # documents 集合索引（无条件挂载：卡死扫描/列表查询/唯一性都依赖，
    # 不能像 ensure_incident_indexes 那样锁在告警开关内）
    try:
        await db.ensure_document_indexes()
        logger.info("documents 索引已就绪")
    except Exception as e:
        logger.warning(f"documents 索引创建失败（不影响主服务）: {e}")

    # MCP 监控工具集成：启用后 Agent 加载 ops_monitoring_server 的 query_metrics/query_logs
    if settings.MCP_ENABLED:
        try:
            from app.api.langgraph import get_agent
            agent = get_agent()
            mcp_count = await agent.init_mcp_tools()
            logger.info(f"MCP 工具已加载: {mcp_count} 个")
        except Exception as e:
            logger.warning(f"MCP 工具加载失败（Agent 将仅使用知识库工具）: {e}")

    # 诊断 worker（持久化任务表消费循环）：
    # 1) 启动时捞回上进程遗留任务（僵尸 running → pending），重启不丢诊断
    # 2) 原子认领保证多副本部署时不重复诊断
    worker_task = None
    sweep_task = None
    pred_task = None
    doc_sweep_task = None
    if settings.ALERT_AUTO_DIAGNOSIS_ENABLED:
        try:
            recovered = await db.recover_stale_diagnosis_tasks(settings.DIAG_TASK_STALE_SECONDS)
            if recovered:
                logger.info(f"诊断任务恢复: {recovered} 个遗留任务回到队列")
            await db.ensure_incident_indexes()  # active 事故唯一索引（B2）
            worker_task = asyncio.create_task(alerts.diagnosis_worker_loop())
            logger.info("诊断 worker 已启动（任务表持久化模式）")
            # B6 事故卡死保护：超时活跃事故自动闭案扫描
            sweep_task = asyncio.create_task(alerts.stale_incident_sweep_loop())
            logger.info(f"卡死事故扫描已启动（超时 {settings.INCIDENT_STALE_AUTO_CLOSE_HOURS}h 自动闭案）")
        except Exception as e:
            logger.warning(f"诊断 worker 启动失败（自动诊断不可用，不影响主服务）: {e}")

    # 主动感知/预判风险（方向1）：仅写 ops_risk_* 指标供看板，不发告警不进事故闭环
    if settings.PREDICTION_ENABLED:
        try:
            from app.prediction.infer_status import risk_prediction_loop
            pred_task = asyncio.create_task(risk_prediction_loop())
            logger.info("预判风险循环已启动（仅提示，不发告警）")
        except Exception as e:
            logger.warning(f"预判循环启动失败（不影响主服务）: {e}")

    # 文档卡死恢复（幂等补偿）：启动捞回 + 周期扫描
    # PENDING/PROCESSING 超时 → FAILED（可经 retry 重投）；deleting 挂起重试向量清理后移除
    try:
        from app.services.document_service import (
            document_stale_sweep_loop,
            recover_stale_documents,
        )
        recovered_docs = await recover_stale_documents()
        if recovered_docs:
            logger.info(f"文档卡死恢复: {recovered_docs} 个标记为失败（可重试）")
        doc_sweep_task = asyncio.create_task(document_stale_sweep_loop())
        logger.info("文档卡死扫描已启动")
    except Exception as e:
        logger.warning(f"文档卡死扫描启动失败（不影响主服务）: {e}")

    yield

    # 关闭时：每个步骤独立 try/except，确保全部执行（防止一个失败导致后续资源泄漏）
    for _t in (sweep_task, worker_task, pred_task, doc_sweep_task):
        if _t:
            try:
                _t.cancel()
            except Exception as e:
                logger.exception(f"取消后台任务失败: {e}")
    from app.core.ai_service import ai_service
    try:
        if hasattr(ai_service, 'close'):
            await ai_service.close()
    except Exception as e:
        logger.exception(f"关闭 AI 服务失败: {e}")
    try:
        await db.disconnect()
    except Exception as e:
        logger.exception(f"关闭数据库连接失败: {e}")
    logger.info("应用已关闭")


# 创建FastAPI应用
app = FastAPI(
    title="知识助手",
    version=settings.APP_VERSION,
    description="个人知识助手 - 支持 RAG 问答、文档管理、记忆系统",
    lifespan=lifespan
)

# 配置CORS：生产环境收窄方法和头，开发环境宽松
if settings.is_production:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ========== request_id 中间件：为每个请求生成唯一 ID，贯穿日志和响应 ==========

class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求注入 request_id：
    - 优先使用客户端传入的 X-Request-ID 头
    - 否则生成 uuid4
    - 写入 contextvar（供 loguru 日志自动携带）
    - 写入响应头 X-Request-ID（便于前端/运维关联）
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_ctx.set(request_id)
        try:
            # loguru 上下文绑定：下游所有 logger 调用自动携带 request_id（轻量链路追踪）
            # ContextVar 实现，跨 await 保留；已知局限：LangGraph ToolNode 独立线程内
            # 的同步工具日志可能丢失绑定（run_in_executor 不复制 contextvars）
            with logger.contextualize(request_id=request_id):
                response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)


app.add_middleware(RequestIDMiddleware)


# ========== 全局异常处理器：防止未捕获异常泄露堆栈给客户端 ==========

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """捕获所有未处理异常，返回统一格式（不泄露堆栈/内部信息给客户端）"""
    rid = request_id_ctx.get()
    logger.bind(request_id=rid).exception(
        f"未捕获异常 | path={request.url.path} | method={request.method} | error={exc}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误，请稍后重试",
            "request_id": rid,
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP 异常也带上 request_id，便于前端/运维关联"""
    rid = request_id_ctx.get()
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "request_id": rid,
        },
        headers=getattr(exc, "headers", None),
    )


# ========== HTTP 指标中间件：请求计数/延迟（AIOps 自观测——Agent 看得见自己） ==========
try:
    from prometheus_client import make_asgi_app
    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False

REQUEST_COUNT = "http_requests_total"
REQUEST_LATENCY = "http_request_duration_seconds"


@app.middleware("http")
async def http_metrics_middleware(request: Request, call_next):
    """记录每个请求的计数与延迟（标签：method/路径模板/status）

    路径取路由模板（如 /api/knowledge/documents/{document_id}）避免高基数；
    未匹配路由的请求退化为原路径（数字段归一）。
    """
    import time as _time
    start = _time.perf_counter()
    response = await call_next(request)
    try:
        route = request.scope.get("route")
        path = getattr(route, "path", None) or re.sub(r"/\d+", "/{id}", request.url.path)
        from app.observability.metrics import get_metrics
        m = get_metrics()
        m.increment(REQUEST_COUNT, 1, labels={
            "method": request.method, "path": path, "status": str(response.status_code),
        })
        m.observe(REQUEST_LATENCY, _time.perf_counter() - start, labels={
            "method": request.method, "path": path,
        })
    except Exception:
        pass  # 指标采集失败不影响请求
    return response


# /metrics 端点：Prometheus 标准暴露格式（prometheus.yml 采集 backend:8000/metrics）
if _HAS_PROMETHEUS:
    app.mount("/metrics", make_asgi_app())


# 注册路由
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(knowledge.router)
app.include_router(health.router)
app.include_router(memory.router)
app.include_router(langgraph.router)  # LangGraph Agent (替代 chat.router)
app.include_router(admin.router)  # 管理后台（仅管理员）
app.include_router(alerts.router)  # Alertmanager webhook Bridge（告警 → 飞书通知）
app.include_router(monitoring.router)  # 监控数据查询 API（前端智能运维看板用）
app.include_router(incidents.router)  # 事故管理（认领/详情——业界 ack 语义）


# 根端点
@app.get("/")
async def root():
    """根端点"""
    return {
        "name": "知识助手",
        "version": settings.APP_VERSION,
        "status": "running",
        "architecture": "RAG + Memory"
    }


# 健康检查（真实实现见 app/api/health.py 的 /api/health 与 /api/health/ready；
# 此处静态版为早期死代码，路由已被 router 版本覆盖，保留会造成"看似有健康检查"的错觉）
@app.get("/api/config")
async def get_config():
    """获取配置信息（仅显示非敏感信息，仅开发环境可用）"""
    # 生产环境直接 404，开发环境也需 DEBUG=true 才可访问（双重保护）
    if settings.is_production or not settings.DEBUG:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "env": settings.ENV,
        "debug": settings.DEBUG,
        "ai_model": settings.AI_MODEL,
        "architecture": "RAG + LangGraph"
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False
    )
