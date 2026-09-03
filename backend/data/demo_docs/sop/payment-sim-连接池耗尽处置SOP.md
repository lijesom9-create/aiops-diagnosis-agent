---
title: payment-sim 连接池耗尽处置 SOP
service: payment-sim
doc_type: sop
effective_date: 2026-08-31
---

# payment-sim 连接池耗尽处置 SOP

## 触发条件

DemoServicePoolSaturation（池借出 ≥5）或 DemoServiceHighErrorRate 伴随
日志出现 `QueuePool limit ... connection timed out`。

## 处置步骤

1. **确认饱和度**：查询 `demo_db_pool_checked_out` 是否持续等于 5；
2. **定位占用方**：连接被谁持有——慢查询（单连接持有时间变长）或泄漏（连接只借不还）；
   - 若伴随 P95 延迟同步上升 → 慢查询持有型，优先查慢查询；
   - 若延迟正常但借出数恒满 → 连接泄漏型，直接重启实例释放；
3. **短期止血**：重启服务实例（池归零，代价是进行中请求中断）；
4. **长期修复**：为慢 SQL 建索引；评估调大 pool_size（需同步评估数据库侧连接上限）。

## 关联指标

`demo_db_pool_checked_out`（饱和度）、`demo_http_request_duration_seconds`（持有时长）、
`demo_payments_total{status="failed"}`（业务损失）。
