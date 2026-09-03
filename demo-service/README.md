# demo-payment-sim —— 被诊断的真实示例服务

> 用途：作为告警驱动诊断 Agent 的**真实诊断对象**。它按真实服务标准实现（真实连接池、RED 指标、结构化日志），配合故障注入工具集，让诊断系统在"真实故障"上被验证——而非 mock 数据。

## 业务面

| 端点 | 说明 |
|------|------|
| `POST /orders` | 下单（`{"item": "...", "amount": 1.0}`），写 SQLite，模拟 5-20ms 处理耗时 |
| `POST /pay/{id}` | 支付（读单→标记已支付→写支付记录） |
| `GET /orders/{id}` | 查单 |
| `GET /healthz` | 存活 + 数据库连通探活 |
| `GET /metrics` | Prometheus 指标（RED 三件套 + 池饱和度 + process 指标） |

## 指标命名（诊断 Agent 需通过知识库文档了解）

- `demo_http_requests_total{method,path,status}` — 请求计数（错误率分母/分子）
- `demo_http_request_duration_seconds_bucket` — 延迟直方图（P95/P99）
- `demo_orders_total` / `demo_payments_total{status}` — 业务量
- `demo_db_pool_checked_out` — 数据库连接池借出数（饱和度，上限 5）
- `process_resident_memory_bytes` — 进程常驻内存（prometheus_client 默认暴露）

## 故障注入控制面（不进入 Prometheus 指标——避免把答案喂给诊断 Agent）

```
POST /_faults/slow_query/on         {"delay": 2.0}    # 慢查询 → P95 延迟告警 + 池压力
POST /_faults/error_storm/on        {}                # /pay 全部 500 → 错误率告警
POST /_faults/pool_exhaustion/on    {}                # 占满 5 个池连接 → 超时 500（先池饱和告警）
POST /_faults/memory_leak/on        {"step_mb": 8}    # 每秒 +8MB → RSS 告警 → OOM 重启
POST /_faults/dependency_timeout/on {"delay": 5.0}    # 下游超时 → 延迟告警
POST /_faults/{name}/off            （解除）
GET  /_faults                       （状态）
```

## 连接池（真实约束的来源）

SQLAlchemy `QueuePool(pool_size=5, max_overflow=0, pool_timeout=3)`：连接被占满后，
第 6 个请求**真实阻塞 3 秒**后抛 `TimeoutError` → 未处理异常 → 500（与生产 HikariCP 耗尽行为同构）。

## 本地运行

```bash
pip install -r requirements.txt
DEMO_DB_PATH=./demo.db SIM_TRAFFIC=1 uvicorn main:app --port 8000
# 自生成流量：~0.5s 一次下单+70% 支付（无需外部压测工具）
```

Docker 部署见根目录 `docker-compose.yml` 的 `demo-service` 服务（mem_limit=256m 使内存泄漏可触发 OOM）。
