# 🤖 个人知识助手 - RAG 问答系统

基于 RAG 的个人知识管理系统，支持文档上传、网页爬取、智能问答。使用 LangGraph 实现 Agent 核心循环，支持自主决策和工具调用。

## ✨ 功能特性

### 📚 知识管理
- **文档上传**：支持 PDF、DOCX、TXT 格式
- **网页爬取**：支持静态页面和 JavaScript 动态页面
- **智能分块**：按段落、标题智能分块，保留文档结构
- **向量化存储**：使用 ChromaDB 持久化存储

### 🔍 RAG 检索
- **混合检索**：关键词 + 向量 + BM25 三路融合
- **CrossEncoder 重排序**：语义重排序，提高检索质量
- **问题理解**：意图识别、问题改写、子问题拆解
- **引用溯源**：答案可追溯到原文

### 🤖 Agent 系统
- **LangGraph Agent**：基于 LangGraph 的 Agent 核心循环
- **自研 Agent**：自研 Agent Loop，支持反思机制
- **工具调用**：搜索、爬取、生成、总结等工具

### 🔧 基础设施
- **MCP 支持**：标准化工具调用协议
- **长期记忆**：用户偏好、历史交互
- **安全权限**：工具调用权限控制
- **可观测性**：链路追踪、指标监控

## 🏗️ 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.10+, FastAPI |
| 前端 | React 18, TypeScript, Tailwind CSS |
| 数据库 | MongoDB |
| 向量数据库 | ChromaDB |
| 嵌入模型 | sentence-transformers (本地) |
| LLM | DeepSeek |
| Agent 框架 | LangGraph |

## 🚀 快速开始

### 前置要求

- Python 3.10+
- Node.js 18+
- MongoDB 6.0+

### 1. 克隆项目

```bash
git clone <repository-url>
cd education-agent
```

### 2. 启动后端

```bash
cd backend

# 创建虚拟环境
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，配置 AI API 密钥

# 启动服务
python main.py
```

后端将运行在 http://localhost:8000

### 3. 启动前端

```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器
npm run dev
```

前端将运行在 http://localhost:3000

### 4. 访问应用

打开浏览器访问 http://localhost:3000，使用以下测试账号登录：

- 用户名：demo2
- 密码：demo123

或者注册新账号。

## 📁 项目结构

```
education-agent/
├── backend/                   # 后端代码
│   ├── app/
│   │   ├── langgraph_agent/  # LangGraph Agent
│   │   ├── api/              # API 路由
│   │   ├── capabilities/     # 能力系统
│   │   ├── core/             # 核心配置
│   │   ├── document/         # 文档处理
│   │   ├── knowledge/        # 知识管理
│   │   ├── mcp/              # MCP 支持
│   │   ├── memory/           # 记忆系统
│   │   ├── observability/    # 可观测性
│   │   ├── retrieval/        # RAG 检索
│   │   ├── security/         # 安全权限
│   │   ├── tools/            # 工具系统
│   │   └── workflow/         # 工作流引擎
│   ├── scripts/              # 脚本工具
│   └── tests/                # 测试
├── frontend/                  # 前端代码
│   └── src/
│       ├── components/       # React 组件
│       ├── services/         # API 服务
│       └── store/            # 状态管理
└── docs/                     # 文档
```

## 🔧 配置说明

### AI 模型配置

在 `backend/.env` 文件中配置 AI 模型：

```bash
# DeepSeek（推荐）
AI_MODEL=deepseek/chat
AI_API_KEY=sk-xxxxxxxxxxxxxxxx
AI_BASE_URL=https://api.deepseek.com

# OpenAI
# AI_MODEL=openai/gpt-4o-mini
# AI_API_KEY=sk-xxxxxxxxxxxxxxxx

# Claude
# AI_MODEL=anthropic/claude-sonnet-4-20250514
# AI_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
```

### 数据库配置

```bash
MONGODB_URL=mongodb://localhost:27017
MONGODB_DB_NAME=education_agent
```

## 📚 API 文档

启动后端后，访问以下地址查看 API 文档：

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

### 核心 API

#### 认证
- `POST /api/auth/register` - 用户注册
- `POST /api/auth/login` - 用户登录
- `GET /api/auth/me` - 获取当前用户

#### LangGraph Agent（主要问答接口）
- `POST /api/langgraph/chat` - 智能问答（支持 RAG + Web Search + 记忆）
- `POST /api/langgraph/chat/stream` - 流式问答
- `GET /api/langgraph/graph` - 获取 Agent 图结构

#### 文档管理
- `POST /api/documents/upload` - 上传文档
- `GET /api/documents/` - 文档列表
- `DELETE /api/documents/{id}` - 删除文档

#### 记忆管理
- `GET /api/memory/profile` - 获取用户画像
- `PUT /api/memory/profile` - 更新用户画像
- `GET /api/memory/entries` - 获取档案记忆
- `POST /api/memory/entries` - 添加档案记忆
- `GET /api/memory/stats` - 记忆统计

## 🛠️ 工具列表

| 工具 | 说明 |
|------|------|
| search_knowledge | 搜索知识库（RAG） |
| web_search | 搜索互联网（Tavily） |
| crawl_webpage | 爬取网页内容 |
| rag_search | RAG 混合检索 |
| summarize_documents | 文档总结 |
| generate_content | 生成内容 |
| get_user_profile | 获取用户画像 |
| save_memory | 保存记忆 |
| search_memory | 搜索记忆 |

## 📊 RAG Pipeline

### 文档摄入

```
文档/网页 → 智能分块 → 向量化 → ChromaDB 存储
```

### 检索流程

```
问题 → 问题理解 → 三路检索 → 融合 → 重排序 → 引用溯源 → 结果
```

### 检索策略

| 策略 | 权重 | 说明 |
|------|------|------|
| 关键词检索 | 50% | 精确匹配 |
| 向量检索 | 30% | 语义匹配 |
| BM25 检索 | 20% | 统计匹配 |

## 🧪 测试

### 运行测试

```bash
cd backend
python -m pytest tests/ -v
```

### 测试结果

```
237 passed, 11 failed（已有问题，与改造无关）
```

## 🚢 部署

### Docker 部署

```bash
# 构建并启动所有服务
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

### 手动部署

参考 `docs/deployment.md` 文档。

## 📈 后续优化

| 方向 | 说明 |
|------|------|
| 前端完善 | 博客写作页面、知识库管理页面 |
| 更多知识源 | GitHub、Notion、飞书接入 |
| 知识图谱 | 实体关系可视化 |
| 多 Agent 协作 | 多个 Agent 协同工作 |
| 产品化 | 用户系统、付费功能 |

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

MIT License

## 🙏 致谢

- [FastAPI](https://fastapi.tiangolo.com/)
- [React](https://react.dev/)
- [LangGraph](https://langchain-ai.github.io/langgraph/)
- [ChromaDB](https://www.trychroma.com/)
- [sentence-transformers](https://www.sbert.net/)
