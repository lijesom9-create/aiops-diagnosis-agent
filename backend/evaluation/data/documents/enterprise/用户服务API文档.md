# 用户服务 API 文档

| 字段 | 值 |
|------|------|
| 文档分类 | api_doc |
| 服务名称 | user-service |
| 当前版本 | v1.2.0 |
| 维护团队 | 平台用户中台 |
| 更新日期 | 2026-07-20 |

## 1. 概述

用户服务（user-service）是公司内部统一的账号与身份管理服务，对接前台电商、内容、SaaS 等多条业务线。本服务基于 FastAPI 0.110 + PostgreSQL 14 + Redis 7 实现，对外提供 RESTful API，所有响应体均为 JSON 格式，字符编码统一为 UTF-8。

## 2. 通用约定

### 2.1 基础信息

- 网关地址：`https://api.internal.company.com`
- 协议：HTTPS（TLS 1.2+）
- 数据格式：`application/json; charset=utf-8`
- 时间格式：ISO 8601（`2026-07-31T08:30:00+08:00`）
- 字符集：所有字符串均使用 UTF-8

### 2.2 认证方式

除"用户注册"、"用户登录"两个接口外，所有接口均需在请求头中携带 Bearer Token：

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMDAxMjMiLCJleHAiOjE3MjI0MDk2MDB9.xxx
```

Token 通过登录接口获取，有效期 2 小时，刷新令牌有效期 7 天。Token 由 JWT 标准签发，使用 HS256 算法签名，载荷（payload）包含 `sub`（用户 ID）、`exp`（过期时间）、`iat`（签发时间）、`scope`（权限范围）等字段。

### 2.3 分页参数

所有列表接口统一使用如下分页参数：

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| page | int | 否 | 1 | 页码，从 1 开始 |
| page_size | int | 否 | 20 | 每页条数，最大 100 |
| sort | string | 否 | created_at:desc | 排序字段:方向，如 `created_at:desc`、`name:asc` |

分页响应统一结构：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "list": [],
    "page": 1,
    "page_size": 20,
    "total": 1580
  }
}
```

### 2.4 通用错误码

| HTTP 状态码 | code | message | 触发场景 |
|-------------|------|---------|----------|
| 400 | 40000 | 参数错误 | 请求参数缺失或格式不正确 |
| 401 | 40100 | 未授权 | Token 缺失、过期或无效 |
| 403 | 40300 | 禁止访问 | 无权限访问该资源 |
| 404 | 40400 | 资源不存在 | 用户或资源不存在 |
| 429 | 42900 | 请求过于频繁 | 触发限流（每分钟 60 次/用户） |
| 500 | 50000 | 服务内部错误 | 服务端未捕获异常 |

### 2.5 统一响应结构

```json
{
  "code": 0,
  "message": "ok",
  "data": {},
  "request_id": "req-3f8b2c9a-20260731-083000"
}
```

`code` 为 0 表示业务成功，非 0 表示业务失败（即使 HTTP 状态码为 200）。`request_id` 用于全链路追踪，问题排查时请提供该字段。

---

## 3. 接口列表

### 3.1 用户注册

- 请求方法：`POST`
- 路径：`/api/v1/users/register`
- 认证：无需

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| username | string | 是 | 用户名，4-32 字符，仅支持字母数字下划线 |
| password | string | 是 | 密码，8-32 字符，至少包含大小写字母与数字 |
| email | string | 是 | 邮箱，需符合标准格式 |
| phone | string | 否 | 手机号，11 位数字 |
| nickname | string | 否 | 昵称，最长 32 字符 |

#### 请求示例

```bash
curl -X POST https://api.internal.company.com/api/v1/users/register \
  -H "Content-Type: application/json" \
  -d '{
    "username": "alice_2026",
    "password": "Alice@2026",
    "email": "alice@company.com",
    "phone": "13800138000",
    "nickname": "爱丽丝"
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "100123",
    "username": "alice_2026",
    "email": "alice@company.com",
    "nickname": "爱丽丝",
    "created_at": "2026-07-31T08:30:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-083000"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40001 | 用户名已存在 | 该用户名已被注册 |
| 40002 | 邮箱已被注册 | 该邮箱已存在账号 |
| 40003 | 密码强度不足 | 密码不符合复杂度要求 |
| 40004 | 手机号格式错误 | 手机号校验失败 |

---

### 3.2 用户登录

- 请求方法：`POST`
- 路径：`/api/v1/users/login`
- 认证：无需

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| account | string | 是 | 用户名 / 邮箱 / 手机号 任一 |
| password | string | 是 | 用户密码 |
| captcha | string | 否 | 图形验证码，连续失败 3 次后必填 |
| device | string | 否 | 设备标识，用于多端登录管理 |

#### 请求示例

```bash
curl -X POST https://api.internal.company.com/api/v1/users/login \
  -H "Content-Type: application/json" \
  -d '{
    "account": "alice@company.com",
    "password": "Alice@2026",
    "device": "web-chrome-1.0"
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxx",
    "refresh_token": "rft-9a8b7c6d-20260731",
    "expires_in": 7200,
    "token_type": "Bearer",
    "user": {
      "user_id": "100123",
      "username": "alice_2026",
      "nickname": "爱丽丝"
    }
  },
  "request_id": "req-3f8b2c9a-20260731-083015"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40101 | 账号或密码错误 | 凭据不正确 |
| 40102 | 账号已锁定 | 连续失败 5 次锁定 30 分钟 |
| 40103 | 验证码错误 | captcha 不正确或已过期 |
| 40104 | 账号已禁用 | 管理员停用该账号 |

---

### 3.3 获取用户信息

- 请求方法：`GET`
- 路径：`/api/v1/users/{user_id}`
- 认证：需要

#### 路径参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| user_id | string | 是 | 用户 ID |

#### 查询参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| fields | string | 否 | 返回字段过滤，逗号分隔，如 `username,email` |

#### 请求示例

```bash
curl -X GET https://api.internal.company.com/api/v1/users/100123?fields=username,email \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx"
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "100123",
    "username": "alice_2026",
    "email": "alice@company.com",
    "phone": "138****8000",
    "nickname": "爱丽丝",
    "avatar": "https://cdn.company.com/avatar/100123.png",
    "gender": "female",
    "status": "active",
    "created_at": "2026-01-15T10:00:00+08:00",
    "last_login_at": "2026-07-30T22:15:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-083100"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40400 | 用户不存在 | user_id 无效 |
| 40300 | 无权限访问 | 仅本人或管理员可访问完整信息 |

---

### 3.4 更新用户信息

- 请求方法：`PUT`
- 路径：`/api/v1/users/{user_id}`
- 认证：需要（仅本人或管理员）

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| nickname | string | 否 | 昵称 |
| avatar | string | 否 | 头像 URL |
| gender | string | 否 | 性别：male/female/unknown |
| bio | string | 否 | 个人简介，最长 200 字符 |

#### 请求示例

```bash
curl -X PUT https://api.internal.company.com/api/v1/users/100123 \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "nickname": "Alice爱丽丝",
    "gender": "female",
    "bio": "后端工程师 / 摄影爱好者"
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "100123",
    "nickname": "Alice爱丽丝",
    "gender": "female",
    "bio": "后端工程师 / 摄影爱好者",
    "updated_at": "2026-07-31T08:35:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-083500"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40005 | 昵称包含敏感词 | 命中敏感词库 |
| 40006 | 头像 URL 无效 | URL 不在白名单域名 |
| 40300 | 无权限修改 | 仅本人或管理员可修改 |

---

### 3.5 修改密码

- 请求方法：`POST`
- 路径：`/api/v1/users/{user_id}/password`
- 认证：需要

#### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| old_password | string | 是 | 原密码 |
| new_password | string | 是 | 新密码，需满足复杂度要求 |
| logout_all | bool | 否 | 是否踢出其他设备的登录态，默认 true |

#### 请求示例

```bash
curl -X POST https://api.internal.company.com/api/v1/users/100123/password \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "old_password": "Alice@2026",
    "new_password": "Alice@2026New",
    "logout_all": true
  }'
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "user_id": "100123",
    "password_updated_at": "2026-07-31T08:40:00+08:00"
  },
  "request_id": "req-3f8b2c9a-20260731-084000"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40105 | 原密码错误 | old_password 不正确 |
| 40003 | 密码强度不足 | 新密码不符合复杂度要求 |
| 40007 | 新旧密码相同 | 新密码不能与旧密码一致 |

---

### 3.6 用户列表

- 请求方法：`GET`
- 路径：`/api/v1/users`
- 认证：需要（仅管理员）

#### 查询参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| page | int | 否 | 页码，默认 1 |
| page_size | int | 否 | 每页条数，默认 20，最大 100 |
| keyword | string | 否 | 关键词，匹配用户名/邮箱/昵称 |
| status | string | 否 | 状态过滤：active/disabled/locked |
| start_time | string | 否 | 注册起始时间，ISO 8601 |
| end_time | string | 否 | 注册结束时间，ISO 8601 |

#### 请求示例

```bash
curl -X GET "https://api.internal.company.com/api/v1/users?page=1&page_size=20&status=active" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.xxx"
```

#### 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "list": [
      {
        "user_id": "100123",
        "username": "alice_2026",
        "email": "alice@company.com",
        "nickname": "Alice爱丽丝",
        "status": "active",
        "created_at": "2026-01-15T10:00:00+08:00"
      },
      {
        "user_id": "100124",
        "username": "bob_dev",
        "email": "bob@company.com",
        "nickname": "Bob",
        "status": "active",
        "created_at": "2026-01-16T11:30:00+08:00"
      }
    ],
    "page": 1,
    "page_size": 20,
    "total": 1580
  },
  "request_id": "req-3f8b2c9a-20260731-084500"
}
```

#### 业务错误码

| code | message | 说明 |
|------|---------|------|
| 40300 | 无权限访问 | 非管理员调用 |
| 40000 | 参数错误 | 时间格式或 page/page_size 越界 |

---

## 4. 限流策略

| 接口 | 限流维度 | 限流阈值 |
|------|----------|----------|
| 用户注册 | IP | 5 次/分钟 |
| 用户登录 | 账号 + IP | 10 次/分钟 |
| 获取用户信息 | 用户 | 60 次/分钟 |
| 用户列表 | 用户 | 30 次/分钟 |

触发限流时返回 HTTP 429，响应头携带 `X-RateLimit-Reset` 字段，表示限流重置时间（秒级时间戳）。

## 5. 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1.2.0 | 2026-07-20 | 新增 `fields` 字段过滤参数 |
| v1.1.0 | 2026-06-15 | 修改密码接口增加 `logout_all` 选项 |
| v1.0.0 | 2026-04-01 | 首次发布 |

## 6. 联系方式

- 服务 Owner：@platform-user-team
- 紧急值班：用户中台 oncall 群（钉钉）
- 工单系统：https://ticket.company.com → 选择"用户中台"
