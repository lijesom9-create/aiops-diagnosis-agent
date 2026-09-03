---
title: "INC-2026-0901 payment-sim 连接池耗尽导致支付接口批量 500"
service: payment-sim
doc_type: incident
severity: P1
incident_id: INC-2026-0901
occurred_at: 2026-08-31
root_cause: "数据库连接被全部占用，后续请求 QueuePool 等待 3 秒超时"
resolution: "释放占用连接后接口恢复；补充连接池耗尽处置 SOP"
---

# INC-2026-0901 payment-sim 连接池耗尽

## 现象

支付接口（POST /pay）批量返回 500；每个失败请求耗时恰好在 3 秒左右
（等于 pool_timeout）；此前 1 分钟 DemoServicePoolSaturation 已先行触发
（demo_db_pool_checked_out = 5 持续）。

## 关键证据

- `demo_db_pool_checked_out` 恒等于 5（pool_size 上限）
- 失败请求耗时集中在 3.0s 附近（pool_timeout 特征值）
- 日志：`QueuePool limit of size 5 overflow 0 reached, connection timed out`
- 错误率：5xx 占比超过 30%，触发 DemoServiceHighErrorRate

## 根因

数据库连接被业务侧全部占用且未释放；pool_size=5 且 max_overflow=0，
无弹性余量，等待 3 秒后池超时拒绝。

## 处置

释放被占用的连接后，池借出数归零，接口在毫秒级恢复正常。

## 经验

- 池饱和度告警（Saturation）比错误率告警早约 1 分钟——**饱和度是先行指标**，
  响应时优先看 `demo_db_pool_checked_out`；
- 失败请求耗时 ≈ pool_timeout 是连接池耗尽型的指纹特征。
