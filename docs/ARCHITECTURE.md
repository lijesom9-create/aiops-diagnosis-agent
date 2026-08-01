# 企业级知识库问答系统 - 架构设计

> 基于 LangGraph + RAG 的企业级知识库问答系统，支持父子分块混合检索、CrossEncoder 重排序、多级缓存、权限隔离和 Docker 部署。

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Frontend (React + TS)                     │
│         ChatPage | DocumentsPage | LoginPage                │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP + SSE (Cookie 认证)
┌──────────────────────────▼──────────────────────────────────┐
│                    API Layer (FastAPI)                       │
│  /api/langgraph | /api/documents | /api/auth | /api/health  │
│  中间件: CORS | 请求ID追踪 | 限流(30RPM) | 异常处理          │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│               LangGraph Agent (核心)                         │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Agent Loop (agent → tools → reflect → agent)        │    │
│  │  工具: search_knowledge | web_search | memory        │    │
│  └─────────────────────────────────────────────────────┘    │
│  Checkpoint: AsyncSqliteSaver (会话记忆持久化)               │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                  RAG 检索链路                                 │
│  查询重写(三级) → 向量+BM25混合 → RRF融合 → 父块取回          │
│  → CrossEncoder重排序(ONNX) → 关键词过滤 → 缓存              │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                    Storage Layer                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│  │ Qdrant   │ │ MongoDB  │ │ Redis    │ │ SQLite   │       │
│  │(向量存储) │ │(文档元数据)│ │(多级缓存) │ │(Checkpoint)│      │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │
└─────────────────────────────────────────────────────────────┘
```

---

## 核心组件

### 1. LangGraph Agent

**位置**: `backend/app/langgraph_agent/`

**职责**: 智能决策、工具选择、多步推理

**图结构**:
```
START → agent → should_continue → tools → agent → ... → END
                    ↓
              reflection → agent (每3步反思)
```

**特性**:
- 自动选择工具（RAG / Web Search / 记忆）
- 每 3 步反思一次，优化决策
- 会话记忆 SQLite 持久化（AsyncSqliteSaver 延迟初始化）
- 系统提示强制知识类问题调用 search_knowledge

---

### 2. RAG 检索链路

**位置**: `backend/app/knowledge/unified_store.py` + `backend/app/retrieval/`

**文档入库流程**:
```
上传 → 解析(docling/pymupdf/text) → 父子分块 → 向量化(bge-small) → Qdrant存储
                                         ↓
                                   BM25倒排索引构建
```

**查询检索流程**:
```
用户查询
    ↓
查询重写(三级判断: 规则 → 历史缓存 → LLM重写)
    ↓
混合检索: 向量检索(Qdrant) + BM25关键词检索
    ↓
RRF 融合排序
    ↓
父块取回(通过 parent_id 关联)
    ↓
关键词重叠过滤(避免无关父块)
    ↓
CrossEncoder 重排序(ONNX加速, 候选≤max(top_k,6))
    ↓
结果缓存(Redis, TTL 5分钟)
    ↓
返回 top_k 结果
```

**关键组件**:
- `parent_child_chunker.py` - 父子分块（保留 heading_path 元数据）
- `qdrant_store.py` - Qdrant 向量存储（本地 sqlite + mmap 持久化）
- `reranker.py` - CrossEncoder 重排序（ONNX Runtime 加速, PyTorch 降级）
- `llm_query_rewriter.py` - 三级查询重写
- `fusion.py` - RRF 融合算法

---

### 3. 记忆系统

**位置**: `backend/app/memory/`

| 记忆类型 | 作用 | 存储方式 |
|---------|------|---------|
| CoreMemory | 用户画像、Agent 人设 | MongoDB |
| RecallMemory | 对话历史 | SQLite Checkpoint |
| ArchivalMemory | 用户笔记、长期记忆 | Qdrant |

---

### 4. 企业级特性

**位置**: `backend/app/core/`

| 特性 | 文件 | 说明 |
|------|------|------|
| 权限隔离 | `auth.py` + `database.py` | 用户/组织隔离，公共文档可见 |
| 限流 | `rate_limiter.py` | 滑动窗口 30 RPM，支持 Redis 分布式 |
| 多级缓存 | `cache.py` | 检索缓存 + LLM响应缓存 + 查询重写缓存 |
| 安全脱敏 | `sanitizer.py` | 手机号/身份证/API Key 过滤 |
| 文档版本 | `documents.py` | 版本递增，更新时清除旧分块+缓存 |
| 答案置信度 | `langgraph.py` | 低置信度自动重试/重写 query |
| 健康检查 | `health.py` | 轻量 `/live` + 完整 `/health` |
| 熔断器 | `circuit_breaker.py` | 外部服务故障保护 |

---

### 5. Web Search

**位置**: `backend/app/langgraph_agent/tools.py`

**API**: Tavily AI Search

**特性**:
- 当知识库无结果时自动触发
- 返回标题、URL、内容摘要
- 结果作为引用来源显示

---

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/auth/register` | POST | 用户注册 |
| `/api/auth/login` | POST | 用户登录（Cookie + Bearer 双模式） |
| `/api/langgraph/chat` | POST | LangGraph Agent 问答 |
| `/api/langgraph/chat/stream` | POST | 流式问答（SSE） |
| `/api/documents/` | GET | 文档列表（含公共文档） |
| `/api/documents/upload` | POST | 上传文档（默认父子分块） |
| `/api/documents/{id}` | DELETE | 删除文档（清除向量+缓存） |
| `/api/memory/profile` | GET/PUT | 用户画像 |
| `/api/memory/entries` | GET/POST | 档案记忆 |
| `/api/health` | GET | 完整健康检查 |
| `/api/health/live` | GET | 轻量存活检查（Docker HEALTHCHECK） |

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React + TypeScript + Tailwind CSS + Zustand |
| 后端 | FastAPI + LangGraph |
| 向量库 | Qdrant（本地 sqlite + mmap 持久化） |
| 文档库 | MongoDB |
| 缓存 | Redis（检索/LLM响应/限流） |
| 会话持久化 | SQLite（AsyncSqliteSaver） |
| Embedding | BAAI/bge-small-zh-v1.5 (512维) |
| Reranker | BAAI/bge-reranker-base（ONNX Runtime 加速） |
| LLM | DeepSeek API (temperature=0.3, max_tokens=1500) |
| Web Search | Tavily API |
| 部署 | Docker Compose（backend + frontend + mongodb + redis） |

---

## 项目结构

```
education-agent/
├── backend/
│   ├── app/
│   │   ├── api/                    # API 端点
│   │   │   ├── langgraph.py        # Agent 问答 API
│   │   │   ├── documents.py        # 文档管理 API
│   │   │   ├── auth.py             # 认证 API
│   │   │   ├── memory.py           # 记忆管理 API
│   │   │   ├── knowledge.py        # 知识库管理 API
│   │   │   └── health.py           # 健康检查 API
│   │   │
│   │   ├── core/                   # 核心基础设施
│   │   │   ├── config.py           # 配置管理（环境隔离）
│   │   │   ├── auth.py             # JWT 认证（Cookie+Bearer）
│   │   │   ├── database.py         # MongoDB 操作
│   │   │   ├── cache.py            # 多级缓存（Redis/Memory）
│   │   │   ├── rate_limiter.py     # 滑动窗口限流
│   │   │   ├── sanitizer.py        # 敏感信息脱敏
│   │   │   └── circuit_breaker.py  # 熔断器
│   │   │
│   │   ├── langgraph_agent/        # LangGraph Agent
│   │   │   ├── agent.py            # Agent 核心（LLM+工具+反思）
│   │   │   ├── tools.py            # 工具定义
│   │   │   └── state.py            # 状态定义
│   │   │
│   │   ├── knowledge/              # 统一知识存储
│   │   │   └── unified_store.py    # 混合检索+重排序+缓存
│   │   │
│   │   ├── retrieval/              # 检索层
│   │   │   ├── qdrant_store.py     # Qdrant 向量存储
│   │   │   ├── embeddings.py       # BGE Embedding 模型
│   │   │   ├── reranker.py         # CrossEncoder 重排序（ONNX）
│   │   │   ├── llm_query_rewriter.py # 三级查询重写
│   │   │   └── fusion.py           # RRF 融合
│   │   │
│   │   ├── document/               # 文档处理
│   │   │   ├── parent_child_chunker.py # 父子分块
│   │   │   ├── parser.py           # 文档解析
│   │   │   └── uploader.py         # 文档上传
│   │   │
│   │   ├── memory/                 # 记忆系统
│   │   ├── evaluation/             # 评估工具
│   │   └── observability/          # 可观测性（指标）
│   │
│   ├── evaluation/                 # 性能测试与评估
│   │   ├── perf/                   # 性能压测脚本
│   │   └── results/                # 评估报告
│   │
│   ├── tests/                      # 测试用例（64个）
│   ├── Dockerfile
│   └── requirements.txt            # 锁定依赖版本
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   └── App.tsx
│   ├── Dockerfile
│   └── nginx.conf                  # SSE 支持
│
├── docker-compose.yml              # 生产编排
├── docker-compose.dev.yml          # 开发环境
└── .github/workflows/ci.yml        # CI/CD
```

---

## 核心流程

### RAG 问答流程

```
用户问题
    ↓
LangGraph Agent 决策
    ↓
┌─────────────────────┐
│ search_knowledge    │ → 查询重写 → 混合检索 → RRF融合
│                     │ → 父块取回 → CrossEncoder重排序
│                     │ → 缓存检查(Redis)
└────────┬────────────┘
         ↓ (知识库无结果时)
┌─────────────────────┐
│ web_search          │ → Tavily API
└────────┬────────────┘
         ↓
┌─────────────────────┐
│ LLM 生成答案        │ → DeepSeek API (temp=0.3, max_tokens=1500)
│                     │ → LLM响应缓存(10分钟TTL)
└────────┬────────────┘
         ↓
答案脱敏 → 返回答案 + 引用来源
```

### 会话记忆流程

```
用户发送消息
    ↓
AsyncSqliteSaver 加载 Checkpoint（延迟初始化）
    ↓
加载历史消息上下文
    ↓
Agent 处理（包含历史上下文）
    ↓
保存新消息到 SQLite Checkpoint
```

---

## 部署

### 环境变量

```env
# AI 服务
AI_API_KEY=your_deepseek_api_key
AI_MODEL=deepseek-chat

# Web Search
TAVILY_API_KEY=your_tavily_api_key

# 数据库
MONGODB_URL=mongodb://mongodb:27017
REDIS_URL=redis://redis:6379/0

# 向量库
QDRANT_PATH=./data/qdrant

# 环境
ENV=production
SECRET_KEY=your_secret_key
```

### Docker 部署

```bash
# 构建并启动所有服务
docker compose up -d

# 服务列表：
# - frontend (nginx, 80端口)
# - backend (uvicorn, 8000端口)
# - mongodb (27017)
# - redis (6379)
```

### 开发环境

```bash
# 后端
cd backend
python -m uvicorn main:app --reload --port 8000

# 前端
cd frontend
npm run dev
```

---

## 性能指标

| 指标 | 数值 | 说明 |
|------|------|------|
| 检索层 QPS | ~20 | CPU/GIL 瓶颈 |
| 缓存命中 QPS | ~142 | LLM 响应缓存 |
| 新问题端到端 | 6-7s | LLM 生成占主导 |
| 缓存命中端到端 | 0.02s | 跳过检索+LLM |
| rerank 耗时 | ~700ms | ONNX 6候选 |
