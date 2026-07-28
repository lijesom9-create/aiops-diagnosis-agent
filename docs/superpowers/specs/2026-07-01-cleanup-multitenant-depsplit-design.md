# 清理技术债 + 多租户 Phase 1 + 依赖拆分

## 概述

三个并行任务，改造当前单用户知识助手为企业级多租户应用的基础。

---

## 1. 清理技术债

### 1.1 删除失效测试文件

以下 12 个测试文件引用了已删除/不存在的模块，无法通过 pytest 收集，删除：

- `backend/tests/test_models.py` — 引用了 `app.models.step`, `app.models.workflow` 等不存在的模型
- `backend/tests/test_retrieval.py` — 引用了 `app.retrieval.hybrid_search` 不存在的模块
- `backend/tests/test_memory.py`
- `backend/tests/test_evaluation.py`
- `backend/tests/test_integration.py`
- `backend/tests/test_knowledge_new.py`
- `backend/tests/test_learning.py`
- `backend/tests/test_learning_path.py`
- `backend/tests/test_lifecycle.py`
- `backend/tests/test_optimization.py`
- `backend/tests/test_regression_fixes.py`
- `backend/tests/test_e2e_test.py`

### 1.2 更新文档

- `README.md`：删除对已不存在的 `agent/` 和 `agent_v2/` 目录的引用
- `ARCHITECTURE.md`：同上

---

## 2. 多租户改造 (Phase 1)

### 2.1 数据模型

#### Organization（新增）

```
Collection: organizations

{
  "org_id": "org_a1b2c3d4e5f6",       // UUID
  "name": "明德教育",                   // 组织名称，注册时传入
  "owner_id": "user_xxx",              // 创建者用户ID
  "created_at": "2026-07-01T...",
  "updated_at": "2026-07-01T..."
}
```

#### User（扩展现有）

新增字段：

```
{
  ...现有字段,
  "org_id": "org_a1b2c3d4e5f6",       // 所属组织
}
```

#### UserResponse（扩展）

新增返回字段：`org_id`, `org_name`

#### JWT Payload（扩展）

```
{
  "sub": "user_xxx",
  "org_id": "org_a1b2c3d4e5f6",
  "exp": ...
}
```

### 2.2 注册流程

```
POST /api/auth/register
{
  "username": "alice",
  "password": "***",
  "email": "alice@example.com",
  "org_name": "明德教育"        // 新增，必填
}

后端逻辑:
1. 检查用户名唯一性
2. 创建 Organization（org_id 由 uuid 生成）
3. 创建 User（关联 org_id）
4. 生成 JWT（嵌入 org_id）
5. 返回 Token
```

### 2.3 数据隔离策略

采用 **共享数据库 + org_id 字段隔离**（选项 A）。

#### MongoDB

所有业务查询追加 org_id 过滤条件：
```python
# 之前
await self._mongo.documents.find({"user_id": user_id})
# 之后
await self._mongo.documents.find({"org_id": org_id, "user_id": user_id})
```

#### ChromaDB

文档 metadata 增加 `org_id`，检索时通过 `where` 过滤：
```python
# 之前
collection.query(query_embeddings=..., where={"user_id": user_id})
# 之后
collection.query(query_embeddings=..., where={"$and": [{"org_id": org_id}, {"user_id": user_id}]})
```

#### 内存模式（开发/测试）

在 `Database` 类的内存列表查询中同样追加 org_id 过滤。

### 2.4 修改文件清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `backend/app/core/database.py` | 新增 + 修改 | 新增 Organization CRUD（`create_org`, `get_org` 等），所有查询加 org_id 过滤 |
| `backend/app/core/auth.py` | 修改 | `UserCreate` 加 `org_name`；`register_user` 加创建组织逻辑；`create_access_token` 加 org_id 到 payload；`get_current_user` 返回 org 信息 |
| `backend/app/api/auth.py` | 修改 | `RegisterRequest` 加 `org_name` 字段 |
| `backend/app/core/config.py` | 无改动 | 当前配置足够 |
| `backend/app/retrieval/chroma_store.py` | 修改 | ChromaDB 查询加 org_id 过滤 |
| `backend/app/knowledge/unified_store.py` | 修改 | 知识库查询加 org_id 过滤 |

### 2.5 API 变更

| 端点 | 变更 |
|---|---|
| `POST /api/auth/register` | 请求体新增 `org_name: str`（必填） |
| `GET /api/auth/me` | 响应新增 `org_id`, `org_name` |
| `GET /api/auth/verify` | 响应新增 `org_id`, `org_name` |

现有业务 API 无需改动端点签名，后端自动从 JWT 提取 org_id 做隔离。

### 2.6 不涉及的范围

- 不修改前端注册页面（当前无组织注册 UI，可后续补）
- 不做跨租户共享/协作（Phase 2 的内容）
- 不做租户管理后台（当前只做底层隔离）
- 不做邀请机制（Phase 2 的内容）

---

## 3. 依赖拆分

### 3.1 文件结构

| 文件 | 用途 | CI 安装 |
|---|---|---|
| `requirements.txt` | 核心运行 + 测试依赖 | ✅ 安装 |
| `requirements-ml.txt` | ML 可选（chromadb, sentence-transformers, torch 等） | ❌ 不装 |

### 3.2 requirements.txt（精简后）

保留：
- Web 框架: fastapi, uvicorn, python-multipart
- 数据库: pymongo, motor
- 认证: PyJWT, passlib, bcrypt
- API 客户端: anthropic, openai, httpx
- 文档解析: pypdf, python-docx
- 工具: python-dotenv, pydantic, pydantic-settings, numpy
- WebSocket: websockets
- 日志: loguru
- 测试: pytest, pytest-asyncio

### 3.3 requirements-ml.txt（拆分出去）

```
chromadb>=1.5.0
sentence-transformers>=2.2.0
```

本地开发同时安装两个文件即可。

### 3.4 CI 调整

CI 只执行 `pip install -r requirements.txt`，不再牵涉 ML 重型依赖，避免依赖冲突和长安装时间。
