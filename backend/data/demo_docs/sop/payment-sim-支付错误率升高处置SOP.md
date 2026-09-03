---
title: payment-sim 支付错误率升高处置 SOP
service: payment-sim
doc_type: sop
effective_date: 2026-08-31
---

# payment-sim 支付错误率升高处置 SOP

## 触发条件

DemoServiceHighErrorRate：5xx 占比 > 30% 持续 1 分钟。

## 分诊步骤

1. **先看耗时形态**（最快分流）：
   - 500 且请求耗时 ~3 秒 → 数据库连接池超时（连接池耗尽型，转《连接池耗尽处置 SOP》）；
   - 500 且请求耗时正常（毫秒级） → 业务异常型（支付逻辑报错）；
2. **业务异常型**：查日志中 payment 接口的异常堆栈，确认是支付逻辑还是数据校验；
3. **检查支付结果指标**：`demo_payments_total{status="failed"}` 增速与错误率是否一致；
4. **止血**：业务异常型可回滚最近版本；池耗尽型按连接池 SOP 处理。

## 关联指标

`demo_http_requests_total{status=~"5.."}`、`demo_payments_total{status}`、
`demo_db_pool_checked_out`（排除池型）。
