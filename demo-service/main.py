"""
demo-payment-sim —— 被诊断的真实示例支付服务

设计目标：不是玩具脚本，而是"被诊断的真实对象"——
- 真实的数据库连接池（SQLAlchemy QueuePool，pool_size=5、无溢出、超时 3s），
  连接耗尽时产生与生产一致的错误与日志
- 真实的 RED 指标（prometheus_client：请求计数/错误/延迟直方图）+ 池饱和度 Gauge
- 真实的结构化日志（JSON → stdout → promtail → Loki）
- 内置流量自生成（无外部压测工具也有持续请求，指标/告警可自然触发）
- 故障注入控制面（faults.py）：与业务面分离，注入状态不进入 Prometheus 指标
  ——避免把"答案"喂给诊断 Agent

业务面：POST /orders（下单）、POST /pay/{id}（支付）、GET /orders/{id}（查询）
控制面：GET /healthz、GET /metrics、POST/GET /_faults/*（故障注入）
"""

import os
import random
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel

import db
import faults

SIM_TRAFFIC = os.environ.get("SIM_TRAFFIC", "1") == "1"

# ========== RED 指标 ==========
# 命名前缀 demo_：真实服务的指标命名空间（诊断 Agent 需通过知识库文档了解这些名字，
# 而非 mock 时代的 error_rate/connection_pool_usage——这是"管线真实化适配"的一部分）
REQUESTS = Counter(
    "demo_http_requests_total", "HTTP 请求总数", ["method", "path", "status"]
)
LATENCY = Histogram(
    "demo_http_request_duration_seconds", "HTTP 请求延迟（秒）", ["method", "path"]
)
ORDERS = Counter("demo_orders_total", "下单总数")
PAYMENTS = Counter("demo_payments_total", "支付结果", ["status"])
# 池饱和度 Gauge 归 db.py 定义（连接池属于存储层），此处不重复注册


def _sample_latency_ms() -> float:
    """模拟正常业务处理耗时 5-20ms（真实服务有基线噪声，而非恒定值）"""
    return random.uniform(5, 20)


# ========== 业务模型 ==========

class OrderIn(BaseModel):
    item: str
    amount: float


# ========== 应用 ==========

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    logger.info("demo-payment-sim 启动完成（pool_size={}, 溢出=0, 超时=3s）", db.POOL_LIMIT)
    if SIM_TRAFFIC:
        t = threading.Thread(target=_traffic_loop, daemon=True)
        t.start()
        logger.info("流量自生成已开启（SIM_TRAFFIC=1，~0.5s 一次下单+支付）")
    yield
    logger.info("demo-payment-sim 关闭")


app = FastAPI(title="demo-payment-sim", lifespan=lifespan)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """RED 指标采集（跳过 /metrics 与 /healthz 自身，避免自举膨胀）"""
    start = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    if path not in ("/metrics", "/healthz"):
        duration = time.perf_counter() - start
        REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        LATENCY.labels(request.method, path).observe(duration)
        logger.info(
            "request | {method} {path} -> {status} | {ms:.1f}ms",
            method=request.method, path=path, status=response.status_code,
            ms=duration * 1000,
        )
    return response


# ========== 业务端点 ==========

@app.post("/orders")
def create_order(order: OrderIn):
    """下单：写库（慢查询故障时额外阻塞）"""
    if faults.is_active("dependency_timeout"):
        # 模拟下游依赖（风控服务）超时：请求被拖住
        time.sleep(faults.param("dependency_timeout", "delay", 5.0))
    if faults.is_active("slow_query"):
        time.sleep(faults.param("slow_query", "delay", 2.0))

    order_id = db.create_order(order.item, order.amount)
    ORDERS.inc()
    return {"order_id": order_id, "item": order.item, "amount": order.amount, "status": "created"}


@app.post("/pay/{order_id}")
def pay(order_id: str):
    """支付：读单 → 标记已支付 → 写支付记录（错误风暴故障时直接 500）"""
    if faults.is_active("error_storm"):
        PAYMENTS.labels("failed").inc()
        raise HTTPException(status_code=500, detail="payment processing failed")

    if faults.is_active("slow_query"):
        time.sleep(faults.param("slow_query", "delay", 2.0))

    order = db.get_order(order_id)
    if not order:
        PAYMENTS.labels("not_found").inc()
        raise HTTPException(status_code=404, detail=f"order {order_id} not found")

    db.mark_paid(order_id)
    PAYMENTS.labels("success").inc()
    return {"order_id": order_id, "status": "paid", "amount": order["amount"]}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = db.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"order {order_id} not found")
    return order


# ========== 控制面 ==========

@app.get("/healthz")
def healthz():
    """存活 + 数据库连通（真实探活要碰存储）"""
    db.ping()
    return {"status": "ok", "faults_active": faults.active_names()}


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---- 故障注入控制面（与业务面分离；状态不进入 Prometheus 指标）----

@app.post("/_faults/{name}/on")
def fault_on(name: str, params: dict = None):
    """注入故障。示例：POST /_faults/slow_query/on  body: {"delay": 2.0}"""
    try:
        faults.inject(name, params or {})
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"未知故障类型: {e}")
    logger.warning("故障注入: {name} params={params}", name=name, params=params or {})
    return {"injected": name, "active": faults.active_names()}


@app.post("/_faults/{name}/off")
def fault_off(name: str):
    faults.clear(name)
    logger.warning("故障解除: {name}", name=name)
    return {"cleared": name, "active": faults.active_names()}


@app.get("/_faults")
def fault_list():
    return {"available": faults.AVAILABLE, "active": faults.active_names()}


# ========== 流量自生成 ==========

def _traffic_loop():
    """后台持续产生真实 HTTP 流量——无外部压测工具时指标/告警也能自然流动。

    直接打 HTTP（而非调用函数）：让中间件指标、日志、连接池全部走真实路径。
    """
    import json
    import urllib.request

    base = "http://localhost:8000"
    n = 0
    while True:
        n += 1
        try:
            req = urllib.request.Request(
                f"{base}/orders",
                data=json.dumps({"item": f"sku-{n % 50}", "amount": round(random.uniform(1, 500), 2)}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                order_id = json.loads(resp.read())["order_id"]
            if random.random() < 0.7:
                urllib.request.urlopen(f"{base}/pay/{order_id}", data=b"", method="POST", timeout=30)
        except Exception as e:  # noqa: BLE001  流量生成器自身不允许死亡
            logger.debug("traffic loop error: {err}", err=e)
        time.sleep(0.5)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
