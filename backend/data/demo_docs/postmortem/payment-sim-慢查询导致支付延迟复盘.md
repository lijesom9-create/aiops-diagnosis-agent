---
title: "payment-sim 慢查询导致支付延迟复盘"
service: payment-sim
doc_type: postmortem
severity: P2
occurred_at: 2026-08-31
root_cause: "业务处理中出现慢查询（单笔 2 秒），P95 延迟从 20ms 恶化至 2 秒"
resolution: "定位慢查询并优化；建立延迟基线告警（P95 > 1s）"
---

# payment-sim 慢查询导致支付延迟复盘

## 现象与影响

DemoServiceHighLatency 触发：P95 延迟从基线 ~20ms 恶化至约 2 秒；
下单与支付接口同步变慢；无 5xx（请求最终成功，只是慢）。

## 时间线

1. 业务处理路径中出现慢查询（单笔约 2 秒）；
2. P95 延迟越过 1s 告警线，持续 2 分钟后 DemoServiceHighLatency 触发；
3. 处理期间连接池借出数上升（请求持有连接时间变长）但未达耗尽。

## 根因

单笔业务处理包含慢查询（约 2 秒）。**与连接池耗尽的区分特征**：
延迟高但无 500（请求成功）；池借出数升高但未恒满。

## 处置与预防

- 定位并优化慢查询（索引/改写）；
- 延迟基线告警（P95 > 1s）已在位，可提前于用户感知发现；
- 复盘要点：慢查询是连接池耗尽的前兆形态——两者通过
  `demo_db_pool_checked_out` 是否恒满区分。
