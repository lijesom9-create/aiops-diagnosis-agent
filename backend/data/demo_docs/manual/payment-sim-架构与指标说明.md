---
title: payment-sim 架构与指标说明
service: payment-sim
doc_type: manual
effective_date: 2026-08-31
---

# payment-sim 架构与指标说明

## 服务职责

payment-sim 是支付域的示例服务，提供下单与支付两类核心接口。所有写操作落在 SQLite（WAL 模式），读多写少场景。

## 接口清单

| 接口 | 说明 | 关键依赖 |
|------|------|---------|
| POST /orders | 下单：写 orders 表 | 数据库连接池 |
| POST /pay/{id} | 支付：读单 → 更新状态 → 写 payments 表 | 数据库连接池 |
| GET /orders/{id} | 查询订单 | 数据库连接池 |

## 数据库连接池（关键瓶颈点）

- 实现：SQLAlchemy QueuePool
- **pool_size=5，max_overflow=0（不允许溢出），pool_timeout=3 秒**
- 耗尽行为：第 6 个借出请求阻塞 3 秒后抛 TimeoutError → 未处理 → HTTP 500
- 饱和度可观测：`demo_db_pool_checked_out` Gauge，达到 5 即全满

## 指标清单（PromQL 查询用）

| 指标名 | 类型 | 标签 | 含义 |
|--------|------|------|------|
| demo_http_requests_total | Counter | method, path, status | 请求计数；5xx 错误率 = status=~"5.." 占比 |
| demo_http_request_duration_seconds_bucket | Histogram | method, path | 延迟分布（P95/P99 用 histogram_quantile） |
| demo_orders_total | Counter | — | 下单量 |
| demo_payments_total | Counter | status | 支付结果（success/failed/not_found） |
| demo_db_pool_checked_out | Gauge | — | 连接池借出数（饱和度核心指标） |
| process_resident_memory_bytes | Gauge | — | 进程常驻内存（容器上限 256Mi） |

**基线参考**：正常 P95 延迟约 20ms；错误率接近 0；池借出数通常 0-2。

## 日志特征

JSON 结构化输出（stdout → Loki），每请求一条：method/path/status/耗时。
数据库池超时的标志性报错：`QueuePool limit of size 5 overflow 0 reached, connection timed out`。

## 告警规则（本服务）

DemoServiceHighErrorRate（5xx>30% 1m）/ DemoServiceHighLatency（P95>1s 2m）/
DemoServicePoolSaturation（借出≥5 1m）/ DemoServiceDown / DemoServiceMemoryHigh（RSS>200MB 3m）
