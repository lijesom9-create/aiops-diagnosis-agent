# 智能运维故障诊断 Agent - 架构设计

> 基于 FastAPI + LangGraph + RAG 的企业级运维故障诊断系统。
> 核心思路：**监控取证（实时指标/日志） + 知识库历史经验 双源交叉印证 → 证据驱动的根因诊断 → 结构化处置报告**。

---

## 1. 系统架构

```
┌───────────────┐   ┌──────────────────┐
│  用户端前端    │   │  管理后台前端     │
│  React :3000  │   │  React :8080     │
└──────┬────────┘   └────────┬─────────┘
       │   HTTP + SSE (Cookie 认证)
┌──────▼─────────────────────▼─────────┐
│        API 层 (FastAPI :8000)         │
│  /api/langgraph | /api/documents |   │
│  /api/auth | /api/admin | /api/memory│
│  /api/health | /api/knowledge        │
│  中间件: CORS | request_id | 限流     │
│  防护: Prompt注入检测 | 敏感信息脱敏   │
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│      LangGraph Agent（诊断核心）      │
│  ┌─────────────────────────────┐    │
│  │ agent → tools → reflect     │    │
│  │  Reflexion 循环 + 证据裁判    │    │
│  │  工具: query_metrics/logs   │    │
│  │  (MCP) + search_knowledge   │    │
│  │        + web_search + memory│    │
│  └─────────────────────────────┘    │
│  Checkpoint: AsyncSqliteSaver(SQLite)│
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│        RAG 检索链路                   │
│  查询改写 → 向量+BM25 并行            │
│  → 加权RRF融合 → 父块取回             │
│  → heading_path过滤 → CrossEncoder   │
│  → 缓存 → top_k                      │
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│           存储层                      │
│  Qdrant(向量)│MongoDB(元数据/会话)    │
│  Redis(缓存,可选)│SQLite(Checkpoint)  │
└──────────────────────────────────────┘
```

---

## 2. LangGraph Agent

**位置**：`backend/app/langgraph_agent/`（`agent.py` / `tools.py` / `state.py`）

### 2.1 图结构

```
START → agent ──should_continue──┬→ tools → agent（循环）
                                 ├→ reflect ──┬→ agent（证据不足继续收集）
                                 └→ END       └→ END（证据充分/循环保护）
```

- **agent 节点**（`_call_agent`）：LLM 调用（`bind_tools`），输出 `tool_calls` 或直接作答。tool_calls 存在 → 进入 tools；无 tool_calls → 进入 reflect 裁判
- **tools 节点**：`ToolNode` 执行工具，支持并行工具调用
- **reflect 节点**（`_reflect`）：证据充分性裁判。回答含 `### 现象` 或判定 `EVIDENCE_SUFFICIENT` → 结束；`EVIDENCE_INSUFFICIENT` → 回到 agent 继续收集证据
- **循环保护**：`max_steps=8`、`max_reflections=3`；第 3 次反思仍无诊断报告 → `_build_degraded_diagnosis` 生成低置信度降级报告

### 2.2 Agent 状态（`state.py`）

`AgentState(MessagesState)` 扩展字段：`tools_used`、`tool_results`、`retrieved_docs`（RAG 证据看板）、`citations`、`monitoring_evidence`（监控证据看板）、`diagnosis_report`（结构化诊断报告）、`reflection`、`reflection_count`、`step_count`、`max_steps`。

### 2.3 工具（`tools.py`，9 个 + MCP 2 个）

| 工具 | 类型 | 说明 |
|------|------|------|
| `query_metrics` | 监控 | 查询服务指标（MCP：mock Prometheus；本地 mock 兜底） |
| `query_logs` | 监控 | 查询应用/慢 SQL 日志（MCP：mock Loki；本地 mock 兜底） |
| `search_knowledge` | RAG | 知识库检索，支持 `service` + `doc_type` 精准过滤；低质量自动 LLM 改写重试 |
| `web_search` / `crawl_webpage` | 外部 | Tavily / 网页爬取 |
| `generate_content` | 生成 | 内容生成 |
| `get_user_profile` / `save_memory` / `search_memory` | 记忆 | 用户画像 / 记忆读写 |

**证据看板机制**：`_call_agent` 从 ToolMessage 中提取 RAG 结果 → `state.retrieved_docs`，监控工具结果 → `state.monitoring_evidence`，注入系统提示与反思提示，LLM 直接看到"证据完整性"而非原始工具文本。

### 2.4 MCP 监控工具集成

- **服务端**：`backend/mcp_servers/ops_monitoring_server.py`（FastMCP，stdio 协议），暴露 `query_metrics` / `query_logs`，返回模拟 Prometheus/Loki 数据
- **客户端**：`init_mcp_tools` 通过 `MultiServerMCPClient`（langchain-mcp-adapters）加载，成功后再建图（新增工具数计入 Agent 工具总数）
- **工具名去重**：本地 tools.py 已有 `query_metrics`/`query_logs` mock，MCP 同名工具加载时**剔除本地同名 mock**，避免重复工具名导致 LLM 端 `400: Tool names must be unique`
- **降级保护**：加载超时/失败 → `_mcp_status=failed/timeout`，系统提示注入"监控工具降级"提示（跳过 2A 阶段、置信度最高"中"），不阻塞应用启动
- 依赖：`langchain-mcp-adapters`、`mcp`（必须加入 requirements.txt，否则 Docker 中加载失败）

### 2.5 会话持久化

`MemorySaver` 首用，首次对话惰性切换为 `AsyncSqliteSaver`（`data/langgraph_checkpoints.db`）。上下文按 token 预算裁剪（`RAG_MAX_CONTEXT_TOKENS`）。

---

## 3. RAG 检索链路

**位置**：`backend/app/knowledge/unified_store.py`（`UnifiedKnowledgeStore`）+ `backend/app/retrieval/`

> 线上路径是 `UnifiedKnowledgeStore.hybrid_search_parent_child`。`retrieval/hybrid_retriever.py` 为早期测试对比用休眠代码，已移除。

### 3.1 检索流水线

```
用户查询
  ↓ ① 查询改写
规则增强(默认) | 多轮指代消解(conversation) | LLM MultiQuery(可选)
  ↓ ② 并行混合检索（ThreadPoolExecutor）
Qdrant 向量检索(child) + BM25 关键词检索(jieba 分词倒排)
  ↓ ③ 加权 RRF 融合
vector:bm25 = 1:1，k=60；CLIP 图像向量按 CLIP_FUSION_WEIGHT 融合
  ↓ ④ 父块取回
按 parent_id 聚合，投票加分 score = best_child_score + 0.1*(命中数-1)
  ↓ ⑤ heading_path 相关性过滤/加权
  ↓ ⑥ CrossEncoder 重排序（ONNX，候选 ≤ max(top_k,6)）
  ↓ ⑦ 结果缓存（5 分钟 TTL）
  ↓ 返回 top_k
```

### 3.2 检索过滤机制（pre-filter + 三路统一）

> 2026-08 重构：org_id/user_id 从 Python post-filter 改为 Qdrant pre-filter，三路检索过滤条件统一。

**统一过滤构造**：三路检索（向量/BM25/CLIP）共用 `_build_child_filter`，保证过滤行为一致：

```python
# _build_child_filter = chunk_type=child + 可见性过滤
filter = {
    "$and": [
        {"chunk_type": "child"},                    # 只检索子块
        {"source": source} if source else {},       # 来源过滤
        metadata_filter,                             # 业务元数据(service/doc_type)
        {"$or_empty": {"key": "org_id", "value": org_id}},    # 公共组织 OR 本组织
        {"$or_missing": {"key": "user_id", "value": user_id}}, # 公共用户 OR 本人
    ]
}
```

**$or_empty vs $or_missing**（对应不同存储约定）：

| 操作符 | 字段 | 存储约定 | Qdrant 实现 |
|--------|------|---------|------------|
| `$or_empty` | org_id | `to_chroma` 强制写入，公共文档 `org_id=""` | `should [MatchValue(""), MatchValue(v)]` |
| `$or_missing` | user_id | `cleaned` 移除空值，公共文档无此字段 | `must_not [MatchExcept([v])]` |

> 注：`IsNullCondition` 在 Qdrant local mode 不生效，故 user_id 用 `must_not+MatchExcept` 反向排除方案。

**三路过滤路径**：

| 路 | 过滤方式 | 说明 |
|----|---------|------|
| 向量 | Qdrant pre-filter | filter 传给 `query_points`，ANN 遍历时用 payload 索引过滤 |
| BM25 | Python post-filter | 内存索引不支持原生 filter，用 `_match_metadata_filter`（等价语义） |
| CLIP | Qdrant pre-filter | filter 传给 `search_by_vector`（2026-08 修复：原漏传 filters） |

**为什么 pre-filter**：post-filter 时其他组织文档若语义更近会占满 top_k，过滤后召回不足。pre-filter 在可见文档池里检索，从根本上消除召回损失。向量/CLIP 路保留 Python 层 org/user 过滤兜底（防御性）。

详见 `learning-notes/03-混合检索三路融合.md` 第六章。

### 3.3 查询改写（`_rewrite_query`）

| 模式 | 说明 |
|------|------|
| `enhanced`（默认） | 后缀剥离 + jieba 关键词组合 + 中英同义/缩写扩展，产出多路查询变体 |
| `conversation` | 三层判断：指代词/上下文依赖规则 → chat_history 存在性 → LLM 指代消解改写（超时降级原句） |
| `llm` / `enhanced_llm` | LLM MultiQuery（httpx 同步，LRU 缓存，相似度过滤） |

### 3.4 向量存储（Qdrant）

**双模式**：

| 模式 | 配置 | 场景 |
|------|------|------|
| 本地嵌入式 | `QDRANT_PERSIST_DIR=./data/qdrant_db`（共享客户端防锁冲突） | 本地开发 |
| Server | `QDRANT_HOST` + `QDRANT_PORT`（Docker qdrant 容器，Web UI :6333/dashboard） | Docker 部署 |

- HNSW 中等预设、COSINE 距离、512 维（bge-small-zh-v1.5）
- 集合：`knowledge`（child）、`knowledge_parent`（parent）、`knowledge_clip_image`（多模态）
- 索引字段（payload）：`chunk_type/source/user_id/org_id/document_id/topic_id/parent_id/doc_type/service/severity/incident_id`
- ⚠️ 注意：本地嵌入式模式 `create_payload_index` 无效（Qdrant 库限制），服务端模式才真正建索引；过滤仍可用

### 3.5 BM25 倒排索引

- 内存 `BM25Index` + 磁盘 `data/bm25_index.pkl` 持久化
- 惰性加载：pkl 条数与向量库 child 数一致则加载，否则全量重建
- 文档入库时增量 `add_batch`（只索引子块）；删除时 `remove_document`

### 3.6 重排序（`retrieval/reranker.py`）

`CrossEncoderReranker`：ONNX Runtime 优先（`data/onnx_cache`），PyTorch 降级；模块级单例 + query/doc 对分数缓存。候选动态选取 `max(top_k, 6)`。

### 3.7 缓存层级

| 缓存 | 作用域 | TTL |
|------|--------|-----|
| LLM 响应缓存 | 相同问题跳过 LLM 生成 | 10 分钟 |
| 检索结果缓存 | 相同查询复用检索结果 | 5 分钟 |
| Embedding 缓存 | LRU 2048 + 磁盘 JSONL | 持久 |
| Rerank 分数缓存 | query/doc 对 | 内存 |

---

## 4. 运维诊断工作流（Ops-Specific）

系统提示（`agent.py`）内嵌企业运维诊断人设，强制 5 阶段流程：

```
阶段1 现象理解：提取 service / 错误现象 / 时间范围 / 影响范围
阶段2 证据收集：
  2A 实时监控取证（优先）：query_metrics(service, metric=all) 拿全指标
     → 根据指标定向 query_logs（HikariPool / slow_query / error）
  2B 知识库检索：search_knowledge(service, doc_type=manual|incident|sop|postmortem)
     （诊断问题至少检索 2 次：manual + incident）
阶段3 根因定位：监控证据 + 历史经验交叉印证，给出最可能根因 + 因果链
阶段4 方案生成：短期止血 + 长期修复
阶段5 结构化报告：### 现象 / 证据 / 根因分析 / 处置方案 / 置信度
```

**检索纪律**：诊断时优先 `service + doc_type` 精准过滤，避免全库噪声；监控优先于凭经验检索。

### 运维元数据（frontmatter → chunk metadata）

运维文档（手册/事故/SOP/复盘）头部用 YAML frontmatter 声明业务字段：

```yaml
---
doc_type: incident
service: payment-service
severity: P1
incident_id: INC-2026-001
---
```

- 解析逻辑：`backend/app/document/frontmatter.py`（`parse_frontmatter` / `extract_business_metadata`）
- API 上传 / 批量上传 / 重试 / Celery 任务 四条路径统一注入 `extra_metadata`
- 字段注入每个 chunk 的 metadata → 支撑检索层 `metadata_filter` 精准过滤
- ⚠️ 无 frontmatter 的文档按普通文档入库（无 service/doc_type），Agent 的 `service+doc_type` 过滤将命中不到——上传运维文档务必带 frontmatter

### 监控数据（mock）

`ops_monitoring_server` 内置模拟 Prometheus/Loki 数据集，与种子事故对齐（如 payment-service 连接池耗尽、order-service Redis 内存等），便于离线演示完整诊断链路。

---

## 5. 核心流程

### 5.1 故障诊断问答

```
用户描述故障
  ↓
Prompt注入检测 → 会话校验 → LLM响应缓存检查
  ↓
LangGraph Agent 循环
  query_metrics(现场指标) → query_logs(定向日志) → search_knowledge(历史经验)
  ↓
Reflect 证据裁判（双源交叉印证）
  ↓
结构化诊断报告（现象/证据/根因/处置/置信度）+ 引用溯源
  ↓
脱敏 → 返回 ChatResponse + 保存会话/记忆
```

### 5.2 文档入库

```
上传(PDF/DOCX/TXT/MD，管理员) → frontmatter 解析 → 文档记录入库
  → (USE_CELERY=true 投递 Celery 任务 / 否则 BackgroundTasks)
  → 解析(docling/pymupdf/text) → 父子分块 → 向量化 → Qdrant
  → BM25 索引增量更新 → 状态更新(completed/failed)
```

### 5.3 多轮会话

```
用户消息 → 会话加载(Checkpoint) → 历史上下文注入
  → 查询改写(指代消解) → Agent 处理 → 保存 Checkpoint
  → 首次对话自动生成会话标题
```

---

## 6. 存储层

| 存储 | 用途 | 说明 |
|------|------|------|
| Qdrant | 向量 | child/parent/CLIP 三集合；本地嵌入式或 Server 模式 |
| MongoDB | 文档元数据、用户、会话、任务 | Motor 异步驱动；`education_agent` 库 |
| Redis | 检索/LLM/限流缓存 | 可选；留空降级内存缓存 |
| SQLite | LangGraph Checkpoint | `data/langgraph_checkpoints.db` |

---

## 7. 部署架构

### Docker（生产主路径）

- 服务：`frontend:3000` / `admin-frontend:8080` / `backend:8000` / `qdrant:6333/6334` / `mongodb` / `redis` / `celery-worker`
- 后端 + celery-worker 用 `./backend/.env` + compose 环境覆盖注入容器内地址（`mongodb:27017`、`redis:6379`、`qdrant:6333`）
- Qdrant 用 **Server 模式**（`qdrant/qdrant` 容器，独立 `qdrant-data` volume）
- `./backend/data`、HF 模型缓存以 volume 挂载（离线加载模型，`HF_HUB_OFFLINE=1`）
- **USE_CELERY=true**：后端投递文档任务到 celery-worker（`--pool=solo`）
- 开发 override：源码卷挂载 + uvicorn `--reload`；生产 override：`SECRET_KEY`/`CORS_ORIGINS` 强制校验、MongoDB 不对外、`backend-data` 命名卷

### 本地开发

- Python 3.12 + `requirements.txt`（含 MCP 依赖 `langchain-mcp-adapters`、`mcp`）
- MongoDB 必选；Qdrant 本地嵌入式（`QDRANT_PERSIST_DIR`）或连 Docker qdrant；Redis 可选

---

## 8. 关键配置与参数

见 `backend/app/core/config.py`（pydantic-settings，读 `backend/.env`）。

重点参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `AI_MODEL` / `AI_API_KEY` | deepseek-chat | LLM（OpenAI 兼容） |
| `VECTOR_STORE_BACKEND` | chroma（旧默认）/ `.env` 设为 qdrant | 生产强制 qdrant |
| `QDRANT_PERSIST_DIR` / `QDRANT_HOST/PORT` | 本地目录 / Docker | 嵌入式 vs Server |
| `MCP_ENABLED` | false | 运维监控工具总开关 |
| `USE_CELERY` | false | 异步文档导入 |
| `RAG_TOP_K` / `RAG_CANDIDATE_MULTIPLIER` / `RAG_RRF_K` | 8 / 3 / 60 | 检索参数 |
| `RAG_REWRITE_MODE` | enhanced | 查询改写 |
| `RAG_MAX_CONTEXT_TOKENS` | 6000 | 上下文预算 |
| `MULTIMODAL_ENABLED` / `MULTIMODAL_VECTOR_ENABLED` | false | 多模态开关 |
| `SUPER_ADMIN_USERNAME` | 空 | 超管初始化 |
| `SECRET_KEY` | 开发自动生成 | 生产必填 ≥32 字节 |

---

## 9. 已知注意事项

- **本地 Qdrant 锁**：同一 `QDRANT_PERSIST_DIR` 只能被一个进程打开（`already accessed by another instance`）。测试/多进程场景用 Server 模式或临时目录
- **工具名唯一**：MCP 与本地工具重名必须去重（见 2.4），否则 LLM API 拒绝请求
- **文档 metadata**：运维文档检索依赖 frontmatter 元数据；旧数据（无 service/doc_type）需 `scripts/seed_ops_kb.py` 或重建索引补齐
- **BM25 一致性**：BM25 索引按 child 条数懒加载/重建；向量库与 BM25 数据源不一致时检索会漂移
- **LLM 响应缓存**：命中时 `step_count=0`（设计如此，表示跳过 LLM 调用）；内存缓存，重启即失效
