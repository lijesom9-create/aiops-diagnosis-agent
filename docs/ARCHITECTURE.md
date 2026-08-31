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
│  │ route_intent → agent → tools│    │
│  │  纯 ReAct 循环（无反思节点）   │    │
│  │  工具: query_metrics/logs   │    │
│  │  (MCP) + search_knowledge   │    │
│  │        + web_search + memory│    │
│  └─────────────────────────────┘    │
│  Checkpoint: AsyncSqliteSaver(SQLite)│
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│        RAG 检索链路                   │
│  查询改写 → dense+sparse 并行        │
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

> 2026-08 更新：移除反思节点（reflect），改为纯 ReAct 循环 + route_intent 意图路由。原因见 [09-12-对话学习总结-Agent与生成深挖.md](file:///d:/Dev/Projects/my-code-space/projects/claude-learning/education-agent/learning-notes/09-12-对话学习总结-Agent与生成深挖.md) 二、P2 去反思——反思 LLM 判断非确定易误判（曾导致对非诊断问题误判 + 编造带假监控数据的诊断）。

```
START → route_intent → agent ──should_continue──┬→ tools → agent（循环）
                                                 └→ END（无 tool_calls / max_steps 上限）
```

- **route_intent 节点**（`_route_intent`）：规则关键词判断 intent（diagnosis/qa/unknown），决定用哪套系统提示
- **agent 节点**（`_call_agent`）：LLM 调用（`bind_tools`），输出 `tool_calls` 或直接作答。tool_calls 存在 → 进入 tools；无 tool_calls → END
- **tools 节点**：`ToolNode` 执行工具，支持并行工具调用
- **证据充分性约束**：诊断 prompt 强制 ≥2 次 search_knowledge + 并行监控取证（代码层确定性约束兜底，替代旧版反思裁判）
- **循环保护**：`max_steps=8`；超限自动生成低置信度降级报告（`_build_degraded_diagnosis`）

### 2.2 Agent 状态（`state.py`）

`AgentState(MessagesState)` 扩展字段：`tools_used`、`tool_results`、`retrieved_docs`（RAG 证据看板）、`citations`、`monitoring_evidence`（监控证据看板）、`diagnosis_report`（结构化诊断报告）、`intent`（P1-1 路由结果）、`memory_context`（P1-2 记忆一次性组装结果）、`step_count`、`max_steps`。

### 2.3 工具（`tools.py`，9 个 + MCP 2 个）

| 工具 | 类型 | 说明 |
|------|------|------|
| `query_metrics` | 监控 | 查询服务指标，支持 `time_range` 时间窗（短窗口瞬时值/长窗口均值）（MCP：真实 Prometheus；本地 mock 兜底） |
| `query_logs` | 监控 | 查询应用/慢 SQL 日志（MCP：真实 Loki；本地 mock 兜底） |
| `get_recent_changes` | 变更 | 查询服务最近变更事件（发布/配置变更/扩缩容）——变更先于深挖，不随 MCP 剔除 |
| `get_service_dependencies` | 拓扑 | 查询服务依赖拓扑（downstream/upstream）——跨服务诊断的钥匙；数据源 `data/service_topology.json`（接 APM 只换数据），不随 MCP 剔除 |
| `create_incident_ticket` | 行动 | 创建故障工单（诊断 → 行动闭环），仅 P1/P2 或用户明确要求时调用 |
| `search_knowledge` | RAG | 知识库检索，支持 `service` + `doc_type` 精准过滤；低质量自动 LLM 改写重试 |
| `web_search` / `crawl_webpage` | 外部 | Tavily / 网页爬取 |
| `generate_content` | 生成 | 内容生成 |
| `get_user_profile` / `save_memory` / `search_memory` | 记忆 | 用户画像 / 记忆读写 |

**工具调用审计**：`run()` 结束后将本次所有工具调用（工具名 + 参数截断 + user_id + session_id + intent）写入 Mongo `tool_audit_logs`（`Database.save_tool_audit_log`，无 Mongo 时内存降级）——满足"哪些数据发给了外部 LLM"的企业安全评审要求。

**证据充分度分（规则校准）**：诊断链路 run 结束后按客观因子计算 0-100 分
（监控取证 30 + 知识库命中 25 + 变更检查 15 + 拓扑检查 10 + 报告完整性 20；
MCP 降级时总分封顶 50），与 LLM 自报置信度并列展示于飞书卡片，
"建议采信级别"取两者中较低者——校准多步工具调用后模型的过度自信。
事故诊断历史同步落库充分度分数，支撑按充分度分层的诊断质量统计。

**应用自观测**：`observability/metrics.py` 以 prometheus_client 为后端
（Counter/Gauge/Histogram 真实分桶，旧自造计数器的无界 history 内存泄漏已修），
`/metrics` ASGI 挂载暴露，prometheus.yml 采集 backend:8000；HTTP 中间件记录
请求计数/延迟（路由模板避免高基数）；Grafana 看板（monitoring/grafana/，8 面板：
QPS/错误率/P95/RAG 各阶段延迟/诊断速率与失败占比）；`/api/health/ready` 真实
探测依赖（liveness/readiness 分离，HEALTHCHECK 弃用恒真 /live）。

**知识新鲜度**：`valid_until` 过期文档在 rerank 后软降权（×0.5，不硬过滤——覆盖优先），
引用列表与 LLM 上下文标注"已过期，仅供参考"；`effective_date` 供 prompt 层
历史结论冲突时"取更新者"。字段随 BUSINESS_FIELDS 走 frontmatter → extra_metadata 全路径。

**证据看板机制**：`_call_agent` 从 ToolMessage 中提取 RAG 结果 → `state.retrieved_docs`，监控工具结果 → `state.monitoring_evidence`，注入系统提示，LLM 直接看到"证据完整性"而非原始工具文本。

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
Qdrant dense 向量检索(child) + sparse 向量（BGE-M3 同源 lexical_weights）
  ↓ ③ 加权 RRF 融合
dense:sparse = 1:1，k=60；CLIP 图像向量默认关闭
  ↓ ④ 父块取回
按 parent_id 聚合，投票加分 score = best_child_score + 0.1*(命中数-1)
  ↓ ⑤ heading_path 相关性过滤/加权
  ↓ ⑥ CrossEncoder 重排序（ONNX int8，候选 ≤ max(top_k,6)）
  ↓ ⑦ 结果缓存（5 分钟 TTL）
  ↓ 返回 top_k
```

### 3.2 检索过滤机制（pre-filter + 三路统一）

> 2026-08 重构：org_id/user_id 从 Python post-filter 改为 Qdrant pre-filter，三路检索过滤条件统一。关键词路从自研 BM25 内存索引改为 Qdrant sparse vector（BGE-M3 lexical_weights 同源）。

**统一过滤构造**：三路检索（dense/sparse/CLIP）共用 `_build_child_filter`，保证过滤行为一致：

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
| dense 向量 | Qdrant pre-filter | filter 传给 `query_points`，ANN 遍历时用 payload 索引过滤 |
| sparse 向量 | Qdrant pre-filter | filter 传给 sparse 查询接口（BGE-M3 同源 lexical_weights，原生 pre-filter） |
| CLIP | Qdrant pre-filter | filter 传给 `search_by_vector`（默认关闭，2026-08 修复：原漏传 filters） |

**为什么 pre-filter**：post-filter 时其他组织文档若语义更近会占满 top_k，过滤后召回不足。pre-filter 在可见文档池里检索，从根本上消除召回损失。dense/CLIP 路保留 Python 层 org/user 过滤兜底（防御性）。

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

- HNSW 中等预设、COSINE 距离、1024 维（BAAI/bge-m3，dense 1024 维 + sparse 同源 lexical_weights）
- 集合：`knowledge`（child）、`knowledge_parent`（parent）、`knowledge_clip_image`（多模态，默认关闭）
- 索引字段（payload）：`chunk_type/source/user_id/org_id/document_id/topic_id/parent_id/doc_type/service/severity/incident_id`
- ⚠️ 注意：本地嵌入式模式 `create_payload_index` 无效（Qdrant 库限制），服务端模式才真正建索引；过滤仍可用

### 3.5 sparse 关键词路（BGE-M3 lexical_weights）

> 2026-08 升级：从自研 BM25 内存索引（`BM25Index` + `data/bm25_index.pkl`）改为 Qdrant sparse vector，使用 BGE-M3 同源 lexical_weights。

- 嵌入模型 `BAAI/bge-m3` 同时输出 dense + sparse 向量，sparse 路不需要单独建倒排索引
- Qdrant 原生支持 sparse vector 的 pre-filter，避免自研索引 Python post-filter 的召回损失
- 文档入库时 dense + sparse 同步写入；删除时一并删除

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
  2A 实时监控取证（优先）：query_metrics(service, metric=all, time_range) 按故障时间窗拿指标
     → 根据指标定向 query_logs（HikariPool / slow_query / error）
  2B 知识库检索：search_knowledge(service, doc_type=manual|incident|sop|postmortem)
     （诊断问题至少检索 2 次：manual + incident）
  2C 变更检查：get_recent_changes(service, hours) 查故障时间窗内变更事件
     （变更是生产故障第一大根因，变更先于深挖）
阶段3 根因定位：监控证据 + 变更事件 + 历史经验交叉印证，给出最可能根因 + 因果链
阶段4 方案生成：短期止血 + 长期修复（P1/P2 可 create_incident_ticket 落地跟进）
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
- **自动分类兜底**：无 frontmatter 时 `infer_business_metadata(filename, title, content_head)` 从文件名/标题/内容推断 doc_type/service（标记 `source=auto_inferred`），在 `uploader._store_chunks` 单点生效，覆盖 API/批量/Celery/降级/种子脚本全部入库路径；service 无高置信命中则不填（宁缺勿错）
- API 上传 / 批量上传 / 重试 / Celery 任务 四条路径统一注入 `extra_metadata`（retry 从 Mongo 文档记录取回 `extra_metadata` 回传，避免重试后 chunk 丢元数据）
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
route_intent 路由（diagnosis 意图）
  ↓
LangGraph Agent 纯 ReAct 循环
  query_metrics(现场指标) → query_logs(定向日志) → get_recent_changes(变更事件)
  → search_knowledge(历史经验)
  （诊断 prompt 强制 ≥2 次 search_knowledge + 监控取证 + 变更检查，代码层约束兜底）
  ↓
结构化诊断报告（现象/证据/根因/处置/置信度）+ 引用溯源 + 工具审计日志
  ↓
脱敏 → 返回 ChatResponse + 保存会话/记忆
```

### 5.1.1 告警自动诊断闭环（Incident 生命周期，push 模式）

诊断挂在**事故实体**上而非单条告警上——告警数与诊断成本脱钩（风暴时 N 条告警聚合为 1 个事故）：

```
Alertmanager webhook（密钥鉴权）→ 告警卡片推送飞书
  ↓ BackgroundTasks
告警路由（_route_alert_to_incident）:
  新 fingerprint → 创建 incident（Mongo incidents）        → 初诊（全量 ReAct）
  同 service 活跃事故 + 关联窗口 → 归入（升级信号）          → 重诊（增量）
  已知 fingerprint 心跳（间隔满足 + 低置信）                 → 重诊（增量）
  resolved 后 FLAPPING_WINDOW 内复燃 → 重新打开原事故        → 不重诊
  ↓ asyncio.Semaphore(1) 全局排队
agent.run(session_id=incident_{id})  → diagnosis_history 落库 → 诊断卡片推送（initial/escalation/repeat 标记）
  ↓ resolved 全部到达
安静期（RESOLVE_QUIET_PERIOD）确认不复燃
  → 恢复摘要（时间线 + 根因确认 + 复盘草稿）→ 绿色卡片 + incident.summary 落库 → 闭案
```

**成本护栏**（默认内置）：重诊硬上限 `DIAG_MAX_REDIAG_PER_INCIDENT`、severity 门槛
`ALERT_MIN_SEVERITY`（LLM 前零成本拦截）、抖动复用 `INCIDENT_FLAPPING_WINDOW`、
增量重诊（上次报告 + 新证据，要求"确认/修正/推翻"三选一，1-2 次调用替代全量）。

**服务映射**：`_extract_service` 按 `service label > job > instance 主机名` 提取，
`ALERT_SERVICE_MAP`（JSON）可静态覆盖——告警规则无 service 标签环境的兜底。

设计取舍：诊断放 BackgroundTasks 仅做入队，执行由**持久化任务表 worker** 消费——
任务落 Mongo `diagnosis_tasks`（FIFO 原子认领 + 失败退避重试 + 上限标 dead + 启动捞回僵尸任务），
重启不丢诊断、多副本不重复消费（find_one_and_update 原子性即跨实例互斥）；
安静期 sleep 在事件循环中不阻塞请求，摘要生成本身入任务表；
重诊共享 incident 会话 checkpoint（`session_id=incident_{id}`）；
中断重试走"继续完成"提示（checkpoint 保留半途现场，不重复取证）；
MCP 未启用时复用 Agent 既有降级提示。

**执行层可靠性三件套**：
1. 任务表（上）——持久化 + 跨实例互斥，替代 Celery 的 90% 收益而无跨进程重建 Agent 成本
2. `CHECKPOINT_BACKEND=mongodb`（compose 生产默认）——会话现场落 Mongo，
   多副本任一实例可恢复同一 incident 会话（SQLite 单文件锁是单副本钉死的主因）
3. 诊断可见性——自动诊断以 `DIAGNOSIS_ORG_ID` 服务身份检索（公共 + 该组织文档），
   文档级 `shared_to_diagnosis` 标记（上传默认 true，敏感文档显式关闭）旁路组织隔离；
   可见性语义 `shared OR (org AND user)`，未共享文档的隔离语义不变（过滤器与 Python
   post-filter 双层一致）

### 5.2 文档入库

```
上传(PDF/DOCX/TXT/MD，管理员) → frontmatter 解析 → 文档记录入库
  → (USE_CELERY=true 投递 Celery 任务 / 否则 BackgroundTasks)
  → 解析(docling/pymupdf/text) → 父子分块 → 向量化（BGE-M3 dense+sparse 同源）→ Qdrant
  → 状态更新(completed/failed)
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
| `ALERT_AUTO_DIAGNOSIS_ENABLED` | true | 告警自动诊断（Incident 生命周期驱动） |
| `DIAG_MAX_REDIAG_PER_INCIDENT` | 3 | 每事故重诊次数硬上限 |
| `ALERT_MIN_SEVERITY` | warning | 触发诊断的最低告警级别 |
| `INCIDENT_FLAPPING_WINDOW` | 1800 | 抖动复用窗口（resolved 后复燃重开原事故） |
| `INCIDENT_RESOLVE_QUIET_PERIOD` | 600 | 恢复摘要安静期 |
| `INCIDENT_SERVICE_JOIN_WINDOW` | 1800 | 新告警归入活跃事故的服务关联窗口 |
| `DIAG_TASK_MAX_ATTEMPTS` | 3 | 诊断任务重试上限（超过标 dead） |
| `DIAG_TASK_RETRY_BACKOFF_SECONDS` | 60 | 失败重试退避窗口 |
| `DIAG_TASK_STALE_SECONDS` | 900 | running 任务僵尸回收窗口（实例崩溃恢复） |
| `DIAGNOSIS_ORG_ID` | 空 | 自动诊断服务身份的组织 ID（检索可见性） |
| `CHECKPOINT_BACKEND` | sqlite | 会话 checkpoint 后端：sqlite / mongodb（多副本必须 mongodb） |
| `AUTH_RATE_LIMIT_PER_MIN` | 20 | 登录/注册按 IP 限流（防暴力破解） |
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
- **sparse/dense 同源**：BGE-M3 同时输出 dense + sparse 向量，入库时同步写入 Qdrant；dense 与 sparse 不再需要单独维护一致性（旧自研 BM25 时代的漂移问题已消除）
- **LLM 响应缓存**：命中时 `step_count=0`（设计如此，表示跳过 LLM 调用）；内存缓存，重启即失效
