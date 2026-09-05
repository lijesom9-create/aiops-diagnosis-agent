# 🤖 智能运维故障诊断 Agent（基于 RAG + LangGraph）

企业级运维故障诊断系统：用户上报线上故障，Agent 依据 **实时监控取证 → 知识库历史经验 → 根因定位 → 处置方案** 的流程进行证据驱动的诊断，输出结构化诊断报告。

后端基于 **FastAPI + LangGraph + RAG**，前端为 React（用户端问答 + 管理后台），向量检索使用 **Qdrant**（本地嵌入式 / Docker Server 两种模式）。

**AIOps 全链路**：Prometheus（指标）+ Loki（日志）+ Alertmanager（告警）+ 飞书自建应用（通知），告警触发 → 自动推送飞书卡片，Agent 可调 MCP 工具三源交叉印证定位根因。

---

## ✨ 核心能力

### 🧠 Agent 故障诊断（LangGraph）
- **意图路由 + ReAct 循环**：`route_intent → agent → tools`，按 query 意图分流（运维诊断/通用/闲聊）
- **5 阶段诊断工作流**（系统提示内置）：现象理解 → 监控取证 → 知识库检索 → 根因定位 → 方案生成
- **结构化诊断报告**：`### 现象 / 证据 / 根因分析 / 处置方案 / 置信度`，答案自动带引用溯源
- **证据充分性约束**：诊断 prompt 强制 ≥2 次 search_knowledge + 并行监控取证（旧版反思裁判节点已移除，改确定性约束兜底）
- **循环保护**：`max_steps=8`，超限自动生成低置信度降级报告，避免无限循环
- **会话记忆**：SQLite Checkpoint（AsyncSqliteSaver）持久化多轮会话

### 🔍 RAG 检索链路
- **父子分离存储**：子块检索、父块取回，保留 heading_path 结构上下文
- **混合检索**：Qdrant dense 向量 + sparse（BGE-M3 同源）并行，加权 RRF 融合
- **CrossEncoder 重排序**：ONNX Runtime 加速（bge-reranker-base，int8 量化优先）
- **查询改写**：规则增强（默认）/ 多轮指代消解 / LLM MultiQuery
- **多模态（可选）**：图片 VLM 描述（qwen-vl-plus）+ OCR + 表格 LLM 摘要；CLIP 图像向量默认关闭

### 🛠️ 运维监控工具（MCP）
- **三 MCP server 并存**（`MCP_SERVER_TYPE=prometheus`）：
  - Prometheus：`query_metrics` / `query_range` 查 PromQL 时序数据（CPU/内存/磁盘/网络/负载）
  - Loki：`query_loki` 查 LogQL 容器日志（支持 container 名模糊匹配 + 关键词过滤）
  - Alertmanager：`query_alerts` 查当前告警列表（firing/pending）
- 诊断时**监控优先**：先取实时指标，再定向查日志，再查 Alertmanager 当前告警，最后查知识库历史经验，**四源交叉印证**
- MCP 子进程显式继承父进程环境变量（`env=os.environ.copy()`），确保容器内 `PROMETHEUS_URL=http://prometheus:9090` 正确传递

### 🚨 AIOps 告警通知链路（端到端已验证）
```
Prometheus 告警规则触发
    ↓
Alertmanager 聚合 / 去重 / 抑制
    ↓
POST /api/alerts/webhook（Bridge 端点，共享密钥鉴权）
    ↓
FeishuClient → 飞书告警卡片（firing 红 / resolved 绿）
    ↓ （Incident 生命周期驱动，BackgroundTasks）
┌─ 初诊：新 fingerprint → 创建事故 → 全量诊断
├─ 重诊：新告警归入（同 service 时间窗）/ 升级 → 增量诊断（确认/修正/推翻）
│        持续 firing 心跳 → 低置信 + 间隔满足时重诊
└─ 恢复摘要：全部 resolved → 安静期确认不复燃 → 时间线 + 根因确认 + 复盘草稿
    ↓
飞书卡片（诊断报告橙色 / 重诊带更新标记 / 恢复摘要绿色）
```
- **事故实体（Incident）**：诊断挂在事故生命周期上而非单条告警——告警数与诊断成本脱钩，风暴时 N 条告警聚合为 1 个事故 1 次初诊
- **成本护栏**（默认内置）：每事故重诊硬上限（`DIAG_MAX_REDIAG_PER_INCIDENT=3`）、severity 门槛（`ALERT_MIN_SEVERITY=warning`，低于门槛零成本拦截）、抖动复用（resolved 后 `INCIDENT_FLAPPING_WINDOW` 内复燃重新打开原事故）、增量重诊（1-2 次调用替代全量 ReAct）
- **执行层可靠性**：诊断任务落 Mongo 任务表（原子认领 + 失败退避重试 + 启动捞回），重启不丢诊断、多副本不重复消费；`CHECKPOINT_BACKEND=mongodb` 会话现场全集群共享；重试走"继续完成"（checkpoint 保留现场，不重复取证）
- **诊断可见性**：自动诊断以 `DIAGNOSIS_ORG_ID` 服务身份检索（公共 + 该组织文档），文档级 `shared_to_diagnosis` 标记（上传默认共享、敏感文档可关闭）旁路组织隔离
- **事故记录落库**：Mongo `incidents` 集合保存指纹集、诊断历史（时间线）、恢复摘要，支撑事后复盘统计
- **测试端点**：`GET /api/alerts/test`（仅管理员）手动触发一条测试告警验证链路
- webhook 需配置 `ALERT_WEBHOOK_SECRET`（未配置时端点拒绝处理），Alertmanager 侧通过 `http_config.authorization` 携带

### 📚 知识库管理
- 支持 PDF / DOCX / TXT / Markdown 上传，智能分块（段落/标题/父子）
- **YAML frontmatter 解析**：文档头部的 `doc_type / service / severity / incident_id` 注入 chunk metadata，支撑 `service + doc_type` 精准过滤
- **自动分类兜底**：无 frontmatter 的文档从文件名/标题/内容开头推断 doc_type/service（标记 `source=auto_inferred` 便于复核），避免缺字段导致检索命中不到
- **知识时效治理**：frontmatter 支持 `valid_until`（过期文档检索软降权 ×0.5，引用标注"仅供参考"）与 `effective_date`（历史结论冲突时取更新者），随上传路径自动注入 chunk metadata
- **21 类多来源知识库**（`backend/data/ops_docs/`）：架构设计 / API 文档 / 配置指南 / 监控告警 / 数据库运维 / 中间件运维 / K8s / 容量规划 / 安全基线 / 变更管理 / 值班手册 / 灾备预案 / 性能调优 / 第三方依赖 / 网络排障 / CI-CD / 数据字典 + 事故 INC-2026-001~100 / 复盘 Postmortem 50 篇 / 手册 / SOP，共 **206 篇**，一键导入脚本
- **Celery 异步导入**：文档解析/向量化异步化，任务状态 + 重试 API

### 📊 应用自观测（运维 Agent 看得见自己）
- prometheus_client 暴露 `/metrics`（HTTP 请求计数/延迟、RAG 各阶段延迟直方图、自动诊断计数）
- Grafana 看板（:3001，8 面板：QPS/错误率/P95/诊断失败占比）+ BackendDown 告警规则
- 真实 readiness 健康检查（`/api/health/ready` 探测 Mongo/Redis/Qdrant，liveness/readiness 分离）

### 🔐 管理与安全
- **管理后台**（独立前端，8080）：文档导入、用户管理、任务监控
- **RBAC**：写操作限定管理员；`SUPER_ADMIN_USERNAME` 超管初始化机制
- **Prompt 注入防护** + 答案敏感信息脱敏 + 限流 + 熔断器

---

## 🏗️ 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.12, FastAPI 0.141+ / LangGraph 1.2+ |
| Agent | LangGraph（意图路由 + ReAct 循环） + MCP (langchain-mcp-adapters) |
| LLM | DeepSeek / Qwen / 任意 OpenAI 兼容接口（`AI_FALLBACK_*` 主备容灾自动切换） |
| 嵌入 | BAAI/bge-m3（本地，dense 1024 维 + sparse 同源） |
| 重排 | BAAI/bge-reranker-base（ONNX Runtime） |
| 向量库 | Qdrant（Docker Server 模式 / 本地嵌入式） |
| 文档库 | MongoDB (Motor) |
| 缓存/队列 | Redis（可选）+ Celery |
| 会话持久化 | SQLite（AsyncSqliteSaver）/ MongoDB（MongoDBSaver，`CHECKPOINT_BACKEND=mongodb`） |
| 前端 | React 18 + TS + Tailwind + Zustand（用户端 3000 / 管理端 8080） |

---

## 🚀 快速开始

### 方式一：Docker（推荐，含 Qdrant Server 模式）

```bash
# 准备环境变量（backend/.env 需存在；开发/生产模板见 backend/.env.*.example）
cp backend/.env.development.example backend/.env   # 或使用生产模板并填入密钥

# 构建并启动全部服务（frontend/backend/admin-frontend/qdrant/mongodb/redis/celery-worker）
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

# 首次部署后导入运维知识库（可选，进入 backend 容器执行或本地脚本）
```

启动后访问：
- 用户端问答：<http://localhost:3000>
- 管理后台：<http://localhost:8080>
- API 文档：<http://localhost:8000/docs>
- Qdrant Web UI：<http://localhost:6333/dashboard>

### 方式二：本地开发

```bash
# 1. 依赖服务：MongoDB（必需）；Qdrant/Redis 可选（无 Redis 用内存缓存，本地 Qdrant 嵌入式）
# 建议用 Docker 起依赖：
docker compose up -d mongodb redis qdrant

# 2. 后端
cd backend
python -m venv venv            # Python 3.12；venv\Scripts\activate（Windows）/ source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # 配置 AI_API_KEY 等
python main.py                 # http://localhost:8000

# 3. 前端（用户端）
cd frontend
npm install
npm run dev                    # http://localhost:3000
```

> ⚠️ 注意：`backend/venv` 若报 `pydantic_core` 类错误，说明解释器与已编译包版本不匹配，请用 **Python 3.12** 重建 venv。

### 运维知识库导入（种子数据）

```bash
cd backend
python scripts/seed_ops_kb.py    # 将 data/ops_docs/ 的 206 篇（21 类）运维文档导入 Qdrant
```

---

## 🧩 系统架构

```
┌───────────────┐   ┌──────────────────┐
│  用户端前端    │   │  管理后台前端     │
│  :3000 (Chat) │   │  :8080 (Admin)   │
└──────┬────────┘   └────────┬─────────┘
       │   HTTP / SSE         │
┌──────▼─────────────────────▼─────────┐
│      API 层 (FastAPI，薄 handler)     │
│  /api/langgraph | /api/documents |   │
│  /api/auth | /api/admin | /api/health│
│  中间件: CORS | 请求ID | 限流 | 异常   │
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│   业务编排层 (app/services/)          │
│  alert_service(告警引擎/事故生命周期) │
│  document_service(文档摄取/处理)      │
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│    LangGraph Agent (核心)            │
│   route_intent → agent → tools → ... │
│   工具: query_metrics/query_logs     │
│        (MCP监控) + search_knowledge   │
│        + web_search + memory         │
│   Checkpoint: AsyncSqliteSaver       │
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│     RAG 检索链路                     │
│  查询改写 → dense+sparse → RRF融合   │
│  → 父块取回 → CrossEncoder重排序     │
│  → 关键词过滤 → 缓存 → top_k          │
└──────┬───────────────────────────────┘
       │
┌──────▼───────────────────────────────┐
│  Qdrant(向量) │ MongoDB(文档元数据)  │
│  Redis(缓存)  │ SQLite(会话Checkpoint)│
└──────────────────────────────────────┘
```

---

## 🔄 RAG 检索流程

```
用户查询
  ↓
查询改写（规则增强 | 多轮指代消解 | LLM MultiQuery）
  ↓
并行混合检索：Qdrant dense 向量(child) + sparse 向量（BGE-M3 同源）
  ↓
加权 RRF 融合（dense:sparse = 1:1, k=60）
  ↓
父块取回（通过 parent_id 关联，投票加分）
  ↓
heading_path 相关性过滤/加权
  ↓
CrossEncoder 重排序（ONNX int8，候选 ≤ max(top_k,6)）
  ↓
结果缓存（Redis/内存，5 分钟 TTL）→ 返回 top_k
```

**关键参数**（`backend/.env` / `app/core/config.py`）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `RAG_TOP_K` | 8 | 最终返回条数（调参实验：8 比 4 提升 recall +0.094） |
| `RAG_CANDIDATE_MULTIPLIER` | 3 | dense/sparse 各返回的候选倍数 |
| `RAG_RRF_K` | 60 | RRF 融合常数 |
| `RAG_REWRITE_MODE` | enhanced | 查询改写模式 |
| `RAG_SEPARATE_PARENT_CHILD` | true | 父子分离存储 |
| `RAG_MAX_CONTEXT_TOKENS` | 6000 | LLM 上下文 token 预算 |

---

## 🤖 Agent 设计

### 图结构

```
START → route_intent → agent → should_continue ─┬→ tools → agent → ...（循环）
                                                 └→ END（无工具调用 / max_steps 上限）
```

- **route_intent**：意图路由（运维诊断 / 通用知识 / 闲聊），分流进入 agent
- **agent**：LLM 调用（绑定工具），输出 tool_calls 或直接回答
- **tools**：ToolNode 执行工具
- **证据充分性**：诊断 prompt 强制 ≥2 次 search_knowledge + 并行监控取证；`max_steps` 超限自动生成低置信度降级报告（旧版反思裁判节点已移除）

### 工具列表

| 工具 | 说明 |
|------|------|
| `query_metrics` | 查询监控指标，支持 `time_range` 时间窗（MCP 真实源 / 本地 mock） |
| `query_logs` | 查询应用/慢 SQL 日志（MCP 真实源 / 本地 mock） |
| `get_recent_changes` | 查询服务最近变更事件（发布/配置/扩缩容）——变更先于深挖 |
| `get_service_dependencies` | 查询服务依赖拓扑（下游依赖/上游调用方）——跨服务诊断；数据源 `data/service_topology.json`（真实接入 APM 时只换数据） |
| `create_incident_ticket` | 创建故障工单（诊断 → 行动闭环，仅 P1/P2 或用户要求时） |
| `analyze_chart` | 分析监控图表（VLM 在线理解） |
| `search_knowledge` | 知识库 RAG 检索（支持 service + doc_type 精准过滤） |
| `web_search` | 互联网搜索（Tavily） |
| `crawl_webpage` | 网页内容爬取 |
| `generate_content` | 内容生成 |
| `get_user_profile` / `save_memory` / `search_memory` | 记忆工具 |

> Agent 的所有工具调用写入审计日志（Mongo `tool_audit_logs`：工具名 + 参数 + 调用者 + 会话），满足"哪些数据发给了外部 LLM"的可追溯要求。

> MCP 工具加载成功后，会剔除本地同名的 `query_metrics`/`query_logs` mock，避免工具名冲突导致 LLM 调用失败。

---

## 📚 API 一览

| 模块 | 端点 | 说明 |
|------|------|------|
| 认证 | `/api/auth/register` `/login` `/logout` `/me` | 注册/登录（Cookie+Bearer） |
| 问答 | `/api/langgraph/chat` | Agent 问答（返回结构化诊断报告） |
| 流式 | `/api/langgraph/chat/stream` | SSE 流式问答 |
| 会话 | `/api/langgraph/sessions*` | 会话 CRUD / 历史消息 |
| 反馈 | `/api/langgraph/feedback` | 答案反馈 + 统计；点踩可带 `document_ids` 触发知识库待复核标记（负反馈 → 质量闭环） |
| 文档 | `/api/documents/upload` `/{id}/status` `/{id}/retry` `/batch-upload` `/{id}` | 上传/状态/重试/批量/删除（写操作需管理员） |
| 知识 | `/api/knowledge/documents/{id}` `/bm25/rebuild` `/stats` | 知识库管理 |
| 管理 | `/api/admin/stats` `/users` `/users/{id}/role` `/tasks` | 统计/用户/角色/任务（仅管理员） |
| 记忆 | `/api/memory/*` | 用户画像 / 档案记忆 |
| 健康 | `/api/health` `/api/health/live` `/api/health/ready` | 健康检查：live 存活探针（恒真）/ ready 就绪探针（真实探测 Mongo/Redis/Qdrant，Docker HEALTHCHECK 使用） |

---

## ⚙️ 配置说明（`backend/.env`）

```env
# AI 模型（OpenAI 兼容格式，provider/model）
AI_MODEL=deepseek-chat
AI_API_KEY=sk-xxx

# 向量存储
VECTOR_STORE_BACKEND=qdrant        # chroma | qdrant（生产强制 qdrant）
# Qdrant 嵌入式（本地开发）：
QDRANT_PERSIST_DIR=./data/qdrant_db
# Qdrant Server 模式（Docker 部署时由 compose 覆盖）：
# QDRANT_HOST=qdrant
# QDRANT_PORT=6333

# MCP 监控工具（运维诊断核心）
MCP_ENABLED=true

# 异步导入（Docker 中由 compose 设为 true）
USE_CELERY=false

# 多模态（可选）
MULTIMODAL_ENABLED=false
MULTIMODAL_VECTOR_ENABLED=false

# 超管初始化（首次注册该用户名自动授予 admin）
SUPER_ADMIN_USERNAME=

# 数据库
MONGODB_URL=mongodb://localhost:27017
REDIS_URL=                       # 留空使用内存缓存
```

完整参数见 [backend/app/core/config.py](backend/app/core/config.py)。

---

## 🧪 测试

```bash
cd backend
python -m pytest tests/ -q                                    # 全量回归
python -m pytest tests/ -q --cov=app --cov-report=term        # 全量 + 覆盖率（app/）
python scripts/e2e_real_test.py                               # 真实 LLM 端到端（需 .env 配置 AI_API_KEY 与 MongoDB，消耗 API 额度）
```

- **当前基线：514 passed / 3 skipped / 0 failed**（全量回归约 7-12 分钟，任何重构/升级后以此为准）
- e2e 覆盖 91 项：`test_api_e2e.py`(43) + `test_e2e_fixes.py`(30) + `test_ops_e2e.py`(18)（运维诊断 /chat 与 /chat/stream 全链路，TestClient + mock，不依赖外部服务）；另有 `scripts/e2e_real_test.py` 真实 LLM + 真实 Mongo 端到端（五阶段：注册/会话/问答/SSE 流式/文档面）
- 质量门禁：`ruff check backend demo-service` 零告警（规则集 E4/E7/E9/F/I/B/ASYNC）；mypy 渐进接入（CI 非阻塞）
- 开发依赖：`pip install -r requirements-dev.txt`（pytest 套件此前为隐式依赖，现已显式化）

主要测试套件：
- `test_agent_graph_flow.py`：Agent 图流转（监控→知识库→诊断报告、循环保护、MCP 降级）
- `test_admin_api.py` / `test_admin_permission.py`：RBAC 与管理 API
- `test_ops_e2e.py`：运维诊断 E2E
- `test_document_upload.py` / `test_security_hardening.py`：文档上传与安全
- `test_ai_failover.py`：AI 供应商主备容灾（错误分类/切换时机/装配，全离线）
- `test_document_lifecycle.py`：文档幂等与卡死恢复（投递补偿/删除挂起/批量隔离）

---

## 📊 评测体系（RAG + Agent Evaluation）

### 检索级评测（250+ 条 / 8 类查询集）

数据集 `backend/evaluation/data/queries_v3_*.json`，覆盖 **普通 / 长尾 / 口语化 / 多跳 / 多模态 / 跨文档 / 多轮 / 负向** 八类共 263 个查询实例，每类含 `expected_keywords / expected_doc_type / cross_doc_types` 等 ground truth：

```bash
cd backend
python evaluation/eval/eval_kb_v3.py [--top-k 8] [--categories normal,long_tail,...]
```

- 指标：Recall@K、MRR、NDCG@K（按类别分项统计）
- 多模态类：图片语义块召回率（image_recall，验证图文统一检索）
- 负向类：无关率（irrelevance rate，验证拒答鲁棒性）
- 多轮类：raw（未消解）vs rewritten（上下文改写近似）对比，验证指代消解价值

### Agent 级评估（端到端诊断质量）

场景集 `backend/evaluation/data/agent_eval_scenarios.json`（12 个，基于真实 Incident 设计），零侵入调用 `agent.run()` 采集 `tools_used / monitoring_evidence / diagnosis_report / citations`：

```bash
cd backend
python evaluation/eval/agent_eval.py [--llm-judge]
# 真实公开事故回放集（Cloudflare 2019 / GitLab 2017 / AWS 2021 / Facebook 2021，
# 评估"知识库无对应历史经验时"的诊断能力边界，报告单独落盘不覆盖主报告）：
python evaluation/eval/agent_eval.py --scenarios-file agent_eval_scenarios_real.json
```

| 指标 | 定义 |
|---|---|
| Root Cause Accuracy | 诊断报告根因与期望根因的关键词命中率（可选 LLM-as-Judge 语义判定） |
| Evidence Recall | 期望证据（监控指标/日志/知识库引用）在 Agent 输出中的覆盖率 |
| Tool Call Accuracy | 期望工具覆盖率 + "监控优先于知识库检索"顺序正确率 |
| Diagnosis Success Rate | 输出结构化诊断报告且置信度合格的占比 |

报告输出：`evaluation/results/kb_v3_eval_report.json`、`agent_eval_report.json`。

---

## 🚢 Docker 部署

```bash
# 生产环境
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# 服务清单
# frontend :3000   admin-frontend :8080   backend :8000
# qdrant :6333/6334   mongodb :27017   redis :6379   celery-worker
```

**部署注意**：
- 后端与 celery-worker 通过 `./backend/.env` + compose 覆盖注入环境变量（容器内用服务名访问 MongoDB/Qdrant/Redis）
- Qdrant 用 **Server 模式**（`qdrant/qdrant` 容器），数据在 Docker volume `qdrant-data`
- `./backend/data` 与 HF 模型缓存以 volume 挂载，避免容器内重复下载模型
- 生产环境务必设置 `SECRET_KEY`（≥32 字节）、`ENV=production`（强制 qdrant + 收窄 CORS）

---

## 🗂️ 项目结构

```
education-agent/
├── backend/
│   ├── main.py                  # FastAPI 入口 + lifespan（路由挂载 / 依赖注入）
│   ├── app/
│   │   ├── api/                 # 薄 handler 层：langgraph / documents / auth / alerts / admin ...
│   │   ├── services/            # 业务编排层：alert_service(告警引擎) / document_service(文档摄取)
│   │   ├── langgraph_agent/     # Agent 核心
│   │   │   ├── agent.py         #   图构建与 ReAct 编排
│   │   │   ├── evidence.py      #   证据解析与组装（纯函数）
│   │   │   ├── prompts.py       #   诊断/QA 系统 prompt 模板
│   │   │   ├── tools.py         #   工具 facade（兼容转发）
│   │   │   ├── tools_retrieval.py / tools_web.py / tools_memory.py / tools_ops.py
│   │   │   ├── retrieval_context.py  # 检索上下文与查询重写（ContextVar / 知识库注入）
│   │   │   ├── tool_cache.py    #   工具结果缓存
│   │   │   └── state.py         #   图状态定义
│   │   ├── knowledge/           # 统一知识存储（unified_store + query_rewrite / store_utils）
│   │   ├── retrieval/           # qdrant_store / embeddings / reranker / fusion ...
│   │   ├── document/            # 上传 / 分块 / 解析 / frontmatter
│   │   ├── tasks/               # Celery 文档导入任务
│   │   ├── core/                # config / database(+db_mixins 按域 Mixin) / cache / prompt_guard ...
│   │   └── memory/              # 记忆系统
│   ├── mcp_servers/             # ops_monitoring_server（FastMCP 监控工具）
│   ├── scripts/                 # seed_ops_kb.py 等
│   ├── data/                    # 向量库 / 模型缓存 / ops_docs 种子库
│   └── tests/
├── frontend/                    # 用户端前端（React + TS）
├── admin-frontend/              # 管理后台（React + TS）
├── docker-compose.yml           # 编排（dev / prod 覆盖文件）
├── docs/代码质量整改记录.md      # 代码质量整改全记录（T1-T10）
└── docs/ARCHITECTURE.md         # 架构设计文档
```

---

## 📈 路线图

已完成：
- ✅ 真实监控源接入（Prometheus + node-exporter + Loki + Promtail）
- ✅ 告警通知闭环（Alertmanager → Bridge → 飞书卡片，端到端验证通过）
- ✅ 管理后台前端（监控看板 / 告警管理 / 日志查询 / 知识库 / 任务 / 用户）
- ✅ 代码质量整改 T1-T10：业务编排层下沉 `app/services/`、巨型文件拆分（database 2009→299、tools 1346→147、documents 940→690）、ruff 零告警、FastAPI 0.141 / LangGraph 1.2 框架升级；全量回归 488 passed 基线不变（详见 [docs/代码质量整改记录.md](docs/代码质量整改记录.md)）

后续方向：
- 多 Agent 协作（监控 Agent / 日志 Agent / 知识 Agent 分工协同）
- 故障自动闭环（诊断 → 工单 → 变更执行）
- 知识图谱（服务依赖 / 故障传播链可视化）
- 接入云监控 API（阿里云云监控 / AWS CloudWatch），扩展多源数据

## 📄 许可证

MIT License，详见 [LICENSE](LICENSE)。
