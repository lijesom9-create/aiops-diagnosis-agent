# 订单服务 API 文档

| 字段 | 值 |
|------|------|
| 文档分类 | api_doc |
| 服务名称 | order-service |
| 当前版本 | v2.3.1 |
| 维护团队 | 交易中台 |
| 更新日期 | 2026-07-25 |

## 1. 概述

订单服务（order-service）是公司电商交易链路的核心服务，承接下单、支付、履约、退款等业务流程。服务基于 Go 1.21 + Gin + MySQL 8.0 + RocketMQ 实现，所有对外接口遵循 RESTful 规范。

## 2. 通用约定

### 2.1 基础信息

- 网关地址：`https://api.trade.company.com`
- 数据格式：`application/json; charset=utf-8`
- 时间格式：ISO 8601，含时区
- 货币单位：分（int 类型），1 元 = 100 分

### 2.2 认证方式

所有接口需在请求头携带 Bearer Token，部分接口还要求商户在请求头携带 `X-Merchant-Id`：

```
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx
X-Merchant-Id: M200001
```

### 2.3 幂等性设计

为防止网络重试或客户端重复提交导致重复下单、重复扣款等问题，所有"写"类接口（创建订单、支付回调、状态流转）必须携带 `idempotency_key`：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| Idempotency-Key | string | 是 | 请求头字段，UUID v4 或业务唯一键，72 小时内服务端去重 |

幂等性处理规则：

1. 服务端将 `idempotency_key` + 请求体哈希作为唯一键，写入 Redis（TTL 72 小时）。
2. 若同一 key 第二次到达，且请求体哈希一致，则直接返回首次请求的响应。
3. 若同一 key 第二次到达，但请求体哈希不一致，返回 HTTP 409，code=40901。
4. 幂等键仅对"写"接口生效，对"读"接口无要求。

```bash
curl -X POST https://api.trade.company.com/api/v1/orders \
  -H "Authorization: Bearer xxx" \
  -H "Idempotency-Key: 7f8b2c9a-2026-0731-083000" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

### 2.4 通用错误码

| HTTP | code | message | 触发场景 |
|------|------|---------|----------|
| 400 | 40000 | 参数错误 | 字段缺失或格式错误 |
| 401 | 40100 | 未授权 | Token 无效 |
| 403 | 40300 | 禁止访问 | 无商户权限 |
| 404 | 40400 | 订单不存在 | order_id 无效 |
| 409 | 40901 | 幂等键冲突 | 同 key 不同请求体 |
| 429 | 42900 | 限流 | 触发限流 |
| 500 | 50000 | 服务内部错误 | 未捕获异常 |

---

## 3. 订单状态机

订单状态枚举如下：

| 状态 | 含义 |
|------|------|
| pending | 待支付：订单创建后初始状态 |
| paid | 已支付：支付成功回调后 |
| shipped | 已发货：商家发货后 |
| delivered | 已签收：用户确认收货或系统自动签收 |
| cancelled | 已取消：用户主动取消或超时未支付 |
| refunded | 已退款：退款流程完成后 |

### 3.1 状态流转图（文字描述）

订单状态机采用有限状态机（FSM）模型，合法流转路径如下：

1. `pending → paid`：用户支付成功，支付回调触发
2. `pending → cancelled`：用户主动取消，或 30 分钟未支付由定时任务自动取消
3. `paid → shipped`：商家发货，调用状态流转接口
4. `shipped → delivered`：物流签收，由物流回调或用户手动确认
5. `paid → refunded`：发货前申请退款
6. `shipped → refunded`：发货后申请退款，需商家同意
7. `delivered → refunded`：签收后 7 天内可申请售后退款

非法的状态流转将被拒绝，返回 HTTP 400，code=40010。例如：`pending → shipped`、`cancelled → paid`、`refunded → paid` 均为非法。

服务端使用状态机引擎（github.com/looplab/fsm）管理流转，所有状态变更通过事务保证一致性，并通过 RocketMQ 发送状态变更事件供下游消费。

---

## 4. 接口列表

### 4.1 创建订单

- 请求方法：`POST`
- 路径：`/api/v1/orders`
- 认证：需要
- 幂等：需要

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| merchant_id | string | 是 | 商户 ID |
| items | array | 是 | 商品列表 |
| items[].sku_id | string | 是 | SKU ID |
| items[].quantity | int | 是 | 数量，1-99 |
| address_id | string | 是 | 收货地址 ID |
| coupon_id | string | 否 | 优惠券 ID |
| remark | string | 否 | 订单备注，最长 200 字符 |
| client_ip | string | 是 | 客户端 IP，用于风控 |

#### 请求示例

```bash
curl -X POST https://api.trade.company.com/api/v1/orders \
  -H "Authorization: Bearer xxx" \
  -H "Idempotency-Key: 7f8b2c9a-2026-0731-083000" \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "M200001",
    "items": [
      {"sku_id": "SKU1001", "quantity": 2},
      {"sku_id": "SKU1002", "quantity": 1}
    ],
    "address_id": "ADDR5001",
    "coupon_id": "CPN200",
    "remark": "周末送达",
    "client_ip": "114.114.114.114"
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "order_id": "OD20260731083000001",
    "status": "pending",
    "total_amount": 29900,
    "discount_amount": 2000,
    "pay_amount": 27900,
    "items": [
      {"sku_id": "SKU1001", "name": "蓝牙耳机", "quantity": 2, "price": 9900},
      {"sku_id": "SKU1002", "name": "无线充电器", "quantity": 1, "price": 10100}
    ],
    "expire_at": "2026-07-31T09:00:00+08:00",
    "created_at": "2026-07-31T08:30:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-083000"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40020 | 商品已下架 | sku_id 不可购买 |
| 40021 | 库存不足 | quantity 超过库存 |
| 40022 | 优惠券不可用 | 过期、不满足门槛或已使用 |
| 40023 | 地址不存在 | address_id 无效 |
| 40901 | 幂等键冲突 | 同 key 不同请求体 |

---

### 4.2 查询订单详情

- 请求方法：`GET`
- 路径：`/api/v1/orders/{order_id}`
- 认证：需要

#### 路径参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| order_id | string | 是 | 订单 ID |

#### 请求示例

```bash
curl -X GET https://api.trade.company.com/api/v1/orders/OD20260731083000001 \
  -H "Authorization: Bearer xxx"
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "order_id": "OD20260731083000001",
    "status": "paid",
    "total_amount": 29900,
    "discount_amount": 2000,
    "pay_amount": 27900,
    "merchant_id": "M200001",
    "items": [
      {"sku_id": "SKU1001", "name": "蓝牙耳机", "quantity": 2, "price": 9900}
    ],
    "address": {
      "name": "张三",
      "phone": "138****8000",
      "detail": "北京市朝阳区xxx"
    },
    "payment": {
      "method": "wechat",
      "paid_at": "2026-07-31T08:35:00+08:00",
      "transaction_id": "wx20260731083500123"
    },
    "logistics": null,
    "created_at": "2026-07-31T08:30:00+08:00",
    "updated_at": "2026-07-31T08:35:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-083500"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40400 | 订单不存在 | order_id 无效 |
| 40300 | 无权限查看 | 非订单所属用户或商户 |

---

### 4.3 订单列表

- 请求方法：`GET`
- 路径：`/api/v1/orders`
- 认证：需要

#### 查询参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| page | int | 否 | 页码，默认 1 |
| page_size | int | 否 | 每页条数，默认 20 |
| status | string | 否 | 状态过滤 |
| start_time | string | 否 | 下单起始时间 |
| end_time | string | 否 | 下单结束时间 |
| merchant_id | string | 否 | 商户过滤（管理员可用） |

#### 请求示例

```bash
curl -X GET "https://api.trade.company.com/api/v1/orders?page=1&page_size=20&status=paid" \
  -H "Authorization: Bearer xxx"
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "list": [
      {
        "order_id": "OD20260731083000001",
        "status": "paid",
        "pay_amount": 27900,
        "created_at": "2026-07-31T08:30:00+08:00"
      }
    ],
    "page": 1,
    "page_size": 20,
    "total": 35
  },
  "request_id": "req-3f8b2c9a-20260731-084000"
}
```

---

### 4.4 取消订单

- 请求方法：`PUT`
- 路径：`/api/v1/orders/{order_id}/cancel`
- 认证：需要
- 幂等：需要

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| reason | string | 是 | 取消原因，枚举：user_change / out_of_stock / timeout / other |
| remark | string | 否 | 备注说明 |

#### 请求示例

```bash
curl -X PUT https://api.trade.company.com/api/v1/orders/OD20260731083000001/cancel \
  -H "Authorization: Bearer xxx" \
  -H "Idempotency-Key: cancel-7f8b2c9a-20260731" \
  -H "Content-Type: application/json" \
  -d '{"reason": "user_change", "remark": "不想买了"}'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "order_id": "OD20260731083000001",
    "status": "cancelled",
    "cancelled_at": "2026-07-31T08:45:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-084500"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40010 | 状态流转非法 | 当前状态不可取消（如已发货） |
| 40011 | 库存回滚失败 | 库存服务异常，需重试 |

---

### 4.5 订单状态流转

- 请求方法：`PUT`
- 路径：`/api/v1/orders/{order_id}/status`
- 认证：需要（商户或后台系统）
- 幂等：需要

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| target_status | string | 是 | 目标状态：paid/shipped/delivered/refunded |
| operator | string | 是 | 操作人 ID |
| operator_name | string | 是 | 操作人姓名 |
| extra | object | 否 | 附加信息，如物流单号 |

#### 请求示例

```bash
curl -X PUT https://api.trade.company.com/api/v1/orders/OD20260731083000001/status \
  -H "Authorization: Bearer xxx" \
  -H "Idempotency-Key: ship-7f8b2c9a-20260731" \
  -H "Content-Type: application/json" \
  -d '{
    "target_status": "shipped",
    "operator": "U2001",
    "operator_name": "李四",
    "extra": {
      "logistics_company": "SF",
      "tracking_no": "SF1234567890"
    }
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "order_id": "OD20260731083000001",
    "previous_status": "paid",
    "current_status": "shipped",
    "updated_at": "2026-07-31T10:00:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-100000"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40010 | 状态流转非法 | 不在合法流转路径中 |
| 40012 | 物流信息缺失 | shipped 状态必须提供 tracking_no |

---

### 4.6 支付回调

- 请求方法：`POST`
- 路径：`/api/v1/orders/payment/callback`
- 认证：需要（支付渠道签名）
- 幂等：需要

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| order_id | string | 是 | 订单 ID |
| transaction_id | string | 是 | 支付渠道交易号 |
| channel | string | 是 | 支付渠道：wechat/alipay/unionpay |
| amount | int | 是 | 实付金额（分） |
| paid_at | string | 是 | 支付完成时间 |
| signature | string | 是 | 渠道签名，用于验签 |

#### 请求示例

```bash
curl -X POST https://api.trade.company.com/api/v1/orders/payment/callback \
  -H "Authorization: Bearer xxx" \
  -H "Idempotency-Key: pay-7f8b2c9a-20260731" \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "OD20260731083000001",
    "transaction_id": "wx20260731083500123",
    "channel": "wechat",
    "amount": 27900,
    "paid_at": "2026-07-31T08:35:00+08:00",
    "signature": "abc123def456"
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "order_id": "OD20260731083000001",
    "status": "paid",
    "processed_at": "2026-07-31T08:35:05+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-083505"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40030 | 签名校验失败 | signature 不正确 |
| 40031 | 金额不匹配 | amount 与订单应付金额不符 |
| 40032 | 订单已支付 | 重复回调（幂等命中则返回成功） |
| 40033 | 订单状态异常 | 订单已取消或已退款 |

---

## 5. 异步事件

订单状态变更后会发送 RocketMQ 消息，下游服务可订阅消费。Topic 命名为 `trade-order-event`，Tag 区分事件类型：

| Tag | 触发时机 | 消息体 |
|-----|----------|--------|
| order_created | 订单创建 | order_id, status=pending |
| order_paid | 支付成功 | order_id, transaction_id |
| order_shipped | 商家发货 | order_id, tracking_no |
| order_cancelled | 订单取消 | order_id, reason |
| order_refunded | 退款完成 | order_id, refund_amount |

消息消费需实现幂等，建议以 `order_id + status` 作为去重键。

## 6. 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v2.3.1 | 2026-07-25 | 修复支付回调金额校验问题 |
| v2.3.0 | 2026-07-01 | 新增幂等性支持 |
| v2.2.0 | 2026-05-15 | 状态流转接口支持 extra 字段 |

## 7. 联系方式

- 服务 Owner：@trade-middleware-team
- 紧急值班：交易中台 oncall 群
- 工单系统：https://ticket.company.com → 选择"交易中台"
