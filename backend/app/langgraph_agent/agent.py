"""
LangGraph Agent

基于 LangGraph 实现的 Agent 系统。
"""

from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from loguru import logger

from .prompts import PROMPT_VERSION, build_diagnosis_prompt, build_qa_prompt

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    HAS_ASYNC_SQLITE = True
except ImportError:
    HAS_ASYNC_SQLITE = False
    logger.warning("langgraph-checkpoint-sqlite 未安装，无法使用 SQLite 持久化")

from ..core.config import settings
from .evidence import (
    _build_citations,
    _build_evidence_summary,
    _collect_retrieved_docs,
    _compute_evidence_sufficiency,
    _extract_monitoring_evidence,
    _merge_monitoring_evidence,
    _merge_retrieved_docs,
    _parse_diagnosis_report,
)
from .state import AgentState
from .tools import (
    create_tools,
    pop_retrieval_buffer,
    set_conversation_context,
    set_current_org_id,
    set_current_user_id,
    set_knowledge_store,
    set_query_rewriter_llm,
)


class LangGraphAgent:
    """
    LangGraph Agent

    基于 LangGraph 实现的 Agent 系统，支持：
    - 自主决策
    - 工具调用
    - RAG 检索
    - 查询路由（诊断 / QA 分流）
    """

    def __init__(
        self,
        llm_model: str = "deepseek-chat",
        llm_base_url: str = "https://api.deepseek.com",
        llm_api_key: str = "dummy",
        max_steps: int = 8,
        knowledge_store=None,
        checkpoint_path: str = None,
        max_context_tokens: int = 32000,
        reserved_for_output: int = 4000,
        llm_fallback: Optional[dict] = None,
    ):
        self.max_steps = max_steps
        self.llm_model = llm_model

        # 读取 LLM 请求超时配置（生产化保护：API 卡住时及时失败，由上层降级）
        from ..core.config import settings as _settings
        llm_timeout = getattr(_settings, "LLM_REQUEST_TIMEOUT", 60.0)

        # 初始化 LLM（支持并行工具调用）
        self.llm = ChatOpenAI(
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0.3,  # 降低温度：生成更稳定且更短，减少耗时
            max_tokens=1500,  # 限制输出长度：避免冗长生成，缩短响应时间
            model_kwargs={"parallel_tool_calls": True},  # 启用并行工具调用
            timeout=llm_timeout,  # 请求超时（秒），超时抛 TimeoutError 由 try/except 降级
        )

        # 初始化工具
        self.tools = create_tools()
        self.tool_node = ToolNode(self.tools)

        # 绑定工具到 LLM；配置了备用模型（AI_FALLBACK_*）时挂 langchain 原生 fallback，
        # 主模型任何失败（鉴权/余额/5xx/超时）自动切换备用，流式与非流式均生效
        self.llm_with_tools = self.llm.bind_tools(self.tools)
        fallback_llm = self._build_fallback_llm(llm_fallback)
        if fallback_llm is not None:
            self.llm_with_tools = self.llm_with_tools.with_fallbacks(
                [fallback_llm.bind_tools(self.tools)]
            )
            logger.info(f"Agent LLM 容灾已启用: 备用模型 {llm_fallback.get('model')}")

        # 设置知识库
        if knowledge_store:
            set_knowledge_store(knowledge_store)

        # 注入查询重写 LLM（复用主 LLM，用于多轮对话指代消解）
        # 用低温度配置，确保重写结果稳定
        from langchain_openai import ChatOpenAI as _ChatOpenAI
        query_rewriter = _ChatOpenAI(
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0.0,  # 确保重写结果稳定一致
            max_tokens=100,   # 重写查询不需要长输出
            timeout=llm_timeout,  # 查询重写同样应用超时保护
        )
        if fallback_llm is not None:
            query_rewriter = query_rewriter.with_fallbacks([fallback_llm])
        set_query_rewriter_llm(query_rewriter)

        # 初始化 Checkpoint（会话记忆）
        # 先用 MemorySaver 临时占位，首次 run() 时延迟切换为 AsyncSqliteSaver
        self.checkpoint_path = checkpoint_path
        self._saver_initialized = False
        self.memory = MemorySaver()
        if checkpoint_path:
            logger.info("Checkpoint 待延迟初始化 SQLite 持久化（首次对话时）")
        else:
            logger.info("Checkpoint 使用内存存储（重启后丢失）")

        # 上下文管理：Token 计数与预算控制
        # DeepSeek/Qwen 上下文窗口约 64K，预留 32K 给上下文 + 4K 给输出
        # 避免长对话消息累积导致超限崩溃
        from ..core.token_counter import TokenCounter
        self.token_counter = TokenCounter(model=llm_model)
        self.max_context_tokens = max_context_tokens
        self.reserved_for_output = reserved_for_output

        # 构建图
        self.graph = self._build_graph()

        # MCP 工具状态（异步加载，在 lifespan/验证脚本里调 init_mcp_tools）
        self._mcp_initialized = False
        self._mcp_tools_count = 0
        self._mcp_client = None  # 持有 MultiServerMCPClient 引用，避免被 GC 回收
        # MCP 加载状态：disabled / loading / success / timeout / failed
        # 用于诊断与降级提示（如监控工具不可用时 Agent 应告知用户而非静默失败）
        self._mcp_status = "disabled"
        self._mcp_last_error: Optional[str] = None

        logger.info(
            f"LangGraph Agent 初始化完成，模型: {llm_model}，"
            f"上下文预算: {max_context_tokens} tokens（预留输出 {reserved_for_output}）"
        )

    def _build_fallback_llm(self, llm_fallback: Optional[dict]):
        """构建备用 ChatOpenAI（AI 容灾）；未配置或配置不完整返回 None

        llm_fallback: {"model": str, "api_key": str, "base_url": str}，
        model/base_url 由 get_agent 按 provider 前缀解析（主配置不带 provider 前缀）。
        """
        if not llm_fallback or not llm_fallback.get("model") or not llm_fallback.get("api_key"):
            return None
        from ..core.config import settings as _settings
        llm_timeout = getattr(_settings, "LLM_REQUEST_TIMEOUT", 60.0)
        return ChatOpenAI(
            model=llm_fallback["model"],
            base_url=llm_fallback.get("base_url") or "https://api.deepseek.com",
            api_key=llm_fallback["api_key"],
            temperature=0.3,
            max_tokens=1500,
            model_kwargs={"parallel_tool_calls": True},
            timeout=llm_timeout,
        )

    async def init_mcp_tools(self, mcp_config: Optional[Dict] = None) -> int:
        """异步加载 MCP Server 的工具，合并到现有工具列表并重建图

        MCP tools 需建立 stdio/http 连接，不能在同步 __init__ 里完成。
        在 web lifespan 或验证脚本里调用本方法。

        幂等：已加载则直接返回。

        生产化保护：
        - 超时保护：加载超过 MCP_LOAD_TIMEOUT 秒后取消，降级为纯知识库模式
        - 状态区分：_mcp_status 记录 disabled/loading/success/timeout/failed，
          供系统提示注入"监控工具不可用"提示，避免 Agent 静默失败
        - 异常隔离：MCP 加载失败不影响 Agent 主流程，原有工具集仍可用

        Args:
            mcp_config: MCP 服务器配置，None 时从 settings 读取并要求 MCP_ENABLED=True。
                格式: {"server_name": {"command": "python", "args": [...], "transport": "stdio"}}

        Returns:
            加载的 MCP 工具数量（0 表示未启用/超时/失败）
        """
        if self._mcp_initialized:
            return self._mcp_tools_count

        # 无显式配置时，从 settings 读取
        if mcp_config is None:
            from ..core.config import settings
            if not getattr(settings, "MCP_ENABLED", False):
                logger.info("MCP 未启用（MCP_ENABLED=False），跳过 MCP 工具加载")
                self._mcp_status = "disabled"
                return 0
            mcp_config = self._build_default_mcp_config()

        # 读取超时配置（生产化保护：MCP server 启动卡住时不阻塞应用）
        from ..core.config import settings as _settings
        mcp_timeout = getattr(_settings, "MCP_LOAD_TIMEOUT", 30.0)

        self._mcp_status = "loading"
        server_names = list(mcp_config.keys())
        logger.info(f"开始加载 MCP 工具，服务器: {server_names}（超时 {mcp_timeout}s）")

        try:
            import asyncio

            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient(mcp_config)
            # 超时保护：get_tools() 内部建立 stdio 连接并枚举工具，
            # 子进程启动慢或无响应时会卡住，用 wait_for 兜底
            try:
                mcp_tools = await asyncio.wait_for(
                    client.get_tools(), timeout=mcp_timeout
                )
            except asyncio.TimeoutError:
                self._set_mcp_status("timeout")
                self._mcp_last_error = f"MCP 加载超时（>{mcp_timeout}s）"
                logger.error(
                    f"MCP 工具加载超时（>{mcp_timeout}s），降级为纯知识库模式。"
                    f"可能原因：server 子进程启动慢、stdio 管道阻塞、server 脚本异常"
                )
                # 尝试关闭半连接的 client（best effort，失败忽略）
                await self._safe_close_mcp_client(client)
                return 0

            if not mcp_tools:
                self._set_mcp_status("empty")
                self._mcp_last_error = "MCP 返回空工具列表"
                logger.warning("MCP 工具加载为空，保留原有工具集")
                return 0

            # 合并 MCP 工具到现有工具列表（保留原有 search_knowledge 等）
            # 去重 + mock 清理：
            # 1) MCP 同名工具优先，剔除本地同名（防 LLM 400: Tool names must be unique）
            # 2) 本地 query_metrics/query_logs/analyze_chart 是写死的 mock 假数据
            #    （payment-service 故障场景），MCP 加载成功后必须剔除，
            #    否则 Agent 会混用真实 Prometheus 指标和 mock 日志，得出错误诊断
            local_tools = create_tools()
            mcp_tool_names = {t.name for t in mcp_tools}
            _MOCK_MONITORING_TOOLS = {"query_metrics", "query_logs", "analyze_chart"}
            self.tools = [
                t for t in local_tools
                if t.name not in mcp_tool_names and t.name not in _MOCK_MONITORING_TOOLS
            ] + mcp_tools
            self.tool_node = ToolNode(self.tools)
            self.llm_with_tools = self.llm.bind_tools(self.tools)

            # 重建图（使用新的工具集）
            self.graph = self._build_graph()

            self._mcp_initialized = True
            self._mcp_tools_count = len(mcp_tools)
            self._mcp_client = client  # 持有引用避免 GC
            self._set_mcp_status("success")
            self._mcp_last_error = None

            tool_names = [t.name for t in mcp_tools]
            logger.info(
                f"MCP 工具加载完成: {len(mcp_tools)} 个 - {tool_names}，"
                f"Agent 工具总数: {len(self.tools)}"
            )
            return len(mcp_tools)

        except Exception as e:
            self._set_mcp_status("failed")
            self._mcp_last_error = str(e)
            logger.error("MCP 工具加载失败（Agent 将仅使用原有工具）: {}", e)
            return 0

    def _set_mcp_status(self, status: str) -> None:
        """更新 MCP 状态并计数（降级可观测：timeout/empty/failed 都是需要关注的降级终态）"""
        self._mcp_status = status
        from ..observability.metrics import safe_increment
        safe_increment("agent_mcp_status_total", 1, labels={"status": status})

    async def _safe_close_mcp_client(self, client) -> None:
        """安全关闭 MCP client（best effort，失败忽略）

        超时或异常后 client 可能处于半连接状态，尝试清理资源。
        不同版本的 langchain-mcp-adapters 关闭接口可能不同，全部容错处理。
        """
        try:
            # 优先调用 close（新版接口）
            if hasattr(client, "close"):
                close_result = client.close()
                if hasattr(close_result, "__await__"):
                    await close_result
            # 兼容旧版 aclose 接口
            elif hasattr(client, "aclose"):
                close_result = client.aclose()
                if hasattr(close_result, "__await__"):
                    await close_result
        except Exception as e:
            logger.debug("关闭 MCP client 失败（忽略）: {}", e)

    @staticmethod
    def _build_default_mcp_config() -> Dict:
        """构建默认 MCP 服务器配置（按 MCP_SERVER_TYPE 选择子进程）

        支持单 server 和多 server 模式：
        - ops_monitoring:     mcp_servers/ops_monitoring_server.py（mock 数据，单 server）
        - system_monitoring:  mcp_servers/system_monitoring_server.py（psutil 查本机指标，单 server）
        - prometheus:         prometheus + loki + alertmanager 三 server（真实指标 + 真实日志 + 真实告警）
          - prometheus:    mcp_servers/prometheus_monitoring_server.py（查 Prometheus HTTP API）
          - loki:          mcp_servers/loki_query_server.py（查 Loki 日志 API）
          - alertmanager:  mcp_servers/alertmanager_query_server.py（查 Alertmanager 告警 API）

        多 server 模式：AIOps 诊断三数据源，
        prometheus 管时序指标，loki 管日志检索，alertmanager 管告警状态。
        各 server 脚本不存在时优雅降级。
        """
        import os

        from ..core.config import settings

        backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        mcp_dir = os.path.join(backend_root, "mcp_servers")
        server_type = getattr(settings, "MCP_SERVER_TYPE", "ops_monitoring")

        # server_type → 脚本文件名映射
        _FILE_MAP = {
            "ops_monitoring": "ops_monitoring_server.py",
            "system_monitoring": "system_monitoring_server.py",
            "prometheus": "prometheus_monitoring_server.py",
        }

        # 显式传递环境变量给 stdio 子进程
        # langchain-mcp-adapters 的 stdio 传输不会自动继承父进程 os.environ，
        # 导致 MCP server 读不到 PROMETHEUS_URL/LOKI_URL/ALERTMANAGER_URL 等配置
        env = os.environ.copy()

        def _server_config(name: str, script: str) -> Dict:
            """构建单个 stdio server 配置"""
            return {
                "command": "python",
                "args": [os.path.join(mcp_dir, script)],
                "transport": "stdio",
                "env": env,
            }

        # prometheus 模式：三 server（指标 + 日志 + 告警）
        if server_type == "prometheus":
            config: Dict[str, Dict] = {}
            # 1. Prometheus server（指标）
            prometheus_script = _FILE_MAP["prometheus"]
            prometheus_path = os.path.join(mcp_dir, prometheus_script)
            if os.path.exists(prometheus_path):
                config["prometheus"] = _server_config("prometheus", prometheus_script)
            else:
                logger.error(f"Prometheus MCP 脚本不存在: {prometheus_path}，无法加载监控工具")

            # 2. Loki server（日志）—— 脚本不存在时跳过，优雅降级
            loki_script = "loki_query_server.py"
            loki_path = os.path.join(mcp_dir, loki_script)
            if os.path.exists(loki_path):
                config["loki"] = _server_config("loki", loki_script)
            else:
                logger.warning(
                    f"Loki MCP 脚本不存在: {loki_path}，降级（无日志查询能力）"
                )

            # 3. Alertmanager server（告警）—— 脚本不存在时跳过，优雅降级
            alertmanager_script = "alertmanager_query_server.py"
            alertmanager_path = os.path.join(mcp_dir, alertmanager_script)
            if os.path.exists(alertmanager_path):
                config["alertmanager"] = _server_config("alertmanager", alertmanager_script)
            else:
                logger.warning(
                    f"Alertmanager MCP 脚本不存在: {alertmanager_path}，降级（无告警查询能力）"
                )

            logger.info(
                f"MCP server 类型: {server_type}, servers: {list(config.keys())}"
            )
            return config

        # 单 server 模式（ops_monitoring / system_monitoring）
        server_file = _FILE_MAP.get(server_type, f"{server_type}_monitoring_server.py")
        server_path = os.path.join(mcp_dir, server_file)

        if not os.path.exists(server_path):
            logger.warning(
                f"MCP server 脚本不存在: {server_path}，回退到 ops_monitoring_server.py"
            )
            server_path = os.path.join(mcp_dir, "ops_monitoring_server.py")
            server_type = "ops_monitoring"

        logger.info(f"MCP server 类型: {server_type}, 脚本: {server_path}")
        return {server_type: _server_config(server_type, os.path.basename(server_path))}

    async def _ensure_saver(self):
        """延迟初始化 checkpointer（需在异步上下文中调用）

        双后端（settings.CHECKPOINT_BACKEND）：
        - sqlite（默认，单机开发）：AsyncSqliteSaver，单文件，多实例会锁冲突
        - mongodb（生产/多副本）：MongoDBSaver（langgraph-checkpoint-mongodb），
          checkpoint 落 Mongo 与事故/任务数据同库，全集群共享会话现场，
          任一实例可恢复同一 incident 的会话——横向扩容的前提
        """
        if self._saver_initialized or not self.checkpoint_path:
            return

        backend = getattr(settings, "CHECKPOINT_BACKEND", "sqlite") or "sqlite"

        if backend == "mongodb":
            try:
                from langgraph.checkpoint.mongodb import MongoDBSaver
                from pymongo import MongoClient
                client = MongoClient(
                    settings.MONGODB_URL, serverSelectionTimeoutMS=5000,
                )
                self.memory = MongoDBSaver(
                    client,
                    db_name=settings.MONGODB_DB_NAME or "education_agent",
                    checkpoint_collection_name="checkpoints",
                )
                self.graph = self._build_graph()
                self._saver_initialized = True
                logger.info(f"Checkpoint 切换为 MongoDB 持久化: db={settings.MONGODB_DB_NAME}")
                return
            except Exception as e:
                logger.warning("MongoDB checkpointer 初始化失败，降级为 MemorySaver: {}", e)
                self._saver_initialized = True
                return

        if not HAS_ASYNC_SQLITE:
            logger.warning("AsyncSqliteSaver 不可用，继续使用 MemorySaver")
            self._saver_initialized = True
            return
        import os
        os.makedirs(os.path.dirname(self.checkpoint_path), exist_ok=True)
        import aiosqlite
        conn = await aiosqlite.connect(self.checkpoint_path)
        # 修复 langgraph 0.4.5 兼容性：setup() 调用 conn.is_alive()，
        # 但 aiosqlite.Connection 没有此方法，手动添加
        if not hasattr(conn, "is_alive"):
            conn.is_alive = lambda: True  # type: ignore
        self.memory = AsyncSqliteSaver(conn)
        await self.memory.setup()
        # 重新编译 graph 以使用新的 checkpointer
        self.graph = self._build_graph()
        self._saver_initialized = True
        logger.info(f"Checkpoint 切换为 SQLite 持久化: {self.checkpoint_path}")

    def _build_graph(self) -> StateGraph:
        """构建 LangGraph 图

        图结构（P2 去反思节点后）：
            [route_intent] → [agent] → {continue: tools, end: END}
                               ↑   ↓
                            [tools]

        诊断与 QA 共用同一套 ReAct 循环，靠 max_steps + prompt 约束保证诊断质量，
        不再用独立反思节点做"证据充分性"二次判断（原反思增加延迟且易误判）。
        """
        # 创建图
        workflow = StateGraph(AgentState)

        # 添加节点
        workflow.add_node("route_intent", self._route_intent)
        workflow.add_node("agent", self._call_agent)
        workflow.add_node("tools", self.tool_node)

        # 设置入口：先路由意图
        workflow.set_entry_point("route_intent")

        # 路由后进入 agent（无论诊断还是 QA 都先走 agent）
        workflow.add_edge("route_intent", "agent")

        # 添加条件边：有工具调用→tools，否则→END
        workflow.add_conditional_edges(
            "agent",
            self._should_continue,
            {
                "continue": "tools",
                "end": END,
            },
        )

        # 工具执行后回到 agent
        workflow.add_edge("tools", "agent")

        # 编译图（添加 checkpoint 支持会话记忆）
        return workflow.compile(checkpointer=self.memory)

    # ========== P1-1: 查询路由 ==========

    # 诊断意图关键词（故障/异常/性能问题）
    # 命中任意关键词 → intent="diagnosis"，走完整诊断工作流（监控优先 + 多步取证）
    # 否则 → intent="qa"，走标准 ReAct，简洁回答
    _DIAGNOSIS_KEYWORDS = frozenset({
        "故障", "报错", "错误", "异常", "崩了", "挂了", "超时", "超时了",
        "慢", "卡", "飙高", "飙升", "泄漏", "oom", "down", "失败",
        "连接池", "连接拒绝", "502", "503", "500", "死锁", "重启",
        "排查", "诊断", "根因", "事故", "告警", "报警", "熔断",
        "cpu", "memory", "内存", "磁盘", "io", "latency", "延迟",
        "unavailable", "timeout", "error", "crash", "hung",
    })

    def _route_intent(self, state: AgentState) -> Dict:
        """查询意图路由（P1-1）

        规则分类：根据用户消息是否含故障/异常关键词判断。
        - diagnosis: 线上故障诊断 → 完整诊断工作流（监控优先 + 多步取证 + 结构化报告）
        - qa: 通用知识/流程/经验问答 → 标准 ReAct，简洁回答

        同时在此节点一次性组装记忆上下文（P1-2），存入 state，
        避免后续 _call_agent 每步重复执行 archival_memory.search。

        设计理由：路由用规则而非 LLM，因为故障关键词匹配的准确率足够高，
        且零延迟零成本。LLM 路由会增加一次 API 调用，对诊断场景延迟敏感。
        """
        # 提取最后一条用户消息
        user_msg = ""
        for msg in reversed(state.get("messages", [])):
            if hasattr(msg, 'type') and msg.type == 'human':
                user_msg = msg.content or ""
                break
            elif hasattr(msg, 'content') and not hasattr(msg, 'tool_calls'):
                user_msg = msg.content or ""
                break

        # 规则分类
        msg_lower = user_msg.lower()
        intent = "diagnosis" if any(kw in msg_lower for kw in self._DIAGNOSIS_KEYWORDS) else "qa"
        logger.info(f"查询路由: intent={intent}, query='{user_msg[:50]}...'")

        # P1-2: 一次性组装记忆上下文
        memory_context = self._build_memory_context(state, user_msg)

        return {
            "intent": intent,
            "memory_context": memory_context,
        }

    # ========== P1-2: 上下文一次性组装 ==========

    def _build_memory_context(self, state: AgentState, user_msg: str) -> str:
        """一次性组装记忆上下文（历史 + 档案记忆）

        在 _route_intent 节点调用一次，结果存入 state.memory_context，
        后续 _build_system_prompt 直接读取，避免每步重复检索（原 _build_system_prompt
        每步都执行 archival_memory.search，多步 Agent 会重复 8 次）。

        Returns:
            组装好的记忆上下文文本（直接拼接到 system prompt）
        """
        task_context = state.get("task_context", {})
        user_id = task_context.get("user_id")
        if not user_id:
            return ""

        try:
            from ..shared_services import get_memory_manager
            memory = get_memory_manager()
            if not memory:
                return ""

            parts = []

            # 最近对话历史
            history = memory.recall_memory.get_recent_history(user_id, limit=5)
            if history:
                lines = []
                for turn in history[-5:]:
                    role_name = "用户" if turn.role == "user" else "AI"
                    lines.append(f"{role_name}: {turn.content[:100]}")
                parts.append("## 最近对话\n" + "\n".join(lines))

            # 相关档案记忆（一次性检索，不重复）
            if user_msg:
                archival = memory.archival_memory.search(user_msg, user_id, top_k=3)
                if archival:
                    lines = []
                    for i, entry in enumerate(archival, 1):
                        cat = f"[{entry.category}] " if entry.category else ""
                        lines.append(f"{i}. {cat}{entry.content[:150]}")
                    parts.append("## 相关记忆\n" + "\n".join(lines))

            return "\n\n".join(parts)
        except Exception as e:
            logger.debug("组装记忆上下文失败: {}", e)
            return ""

    def _call_agent(self, state: AgentState) -> Dict:
        """调用 Agent（LLM）"""
        messages = state["messages"]
        # 续跑/重试时历史可能残留"未被工具执行"的 tool_calls（上轮 max_steps 截断所致），
        # 原样回传 LLM 会触发 OpenAI 400；先中性化（丢弃空壳 / 剥离悬空 tool_calls）
        messages = self._neutralize_unpaired_tool_calls(messages)
        step_count = state.get("step_count", 0)
        tools_used = state.get("tools_used", [])
        task_context = state.get("task_context", {})
        user_id = task_context.get("user_id")

        # 引用溯源：收集检索结果（两个来源合并去重）
        # 1. 模块级 buffer（主要方案：search_knowledge 工具写入）
        buffered_docs = pop_retrieval_buffer()
        # 2. ToolMessage artifact（备用方案：未来 LangGraph 版本可能支持）
        artifact_docs = _collect_retrieved_docs(messages)
        # 合并已有 + 新增，按 doc_id + content 去重
        existing = state.get("retrieved_docs", [])
        retrieved_docs = _merge_retrieved_docs(existing + buffered_docs + artifact_docs)

        # 双源融合：提取监控证据（query_metrics/query_logs 返回的结构化数据）
        # 与知识库证据（retrieved_docs）对称管理，供证据看板注入 prompt
        existing_monitoring = state.get("monitoring_evidence", [])
        new_monitoring = _extract_monitoring_evidence(messages)
        monitoring_evidence = _merge_monitoring_evidence(existing_monitoring + new_monitoring)

        # P3: per-session 数据通过 InjectedState 传递给工具，不再需要全局变量设置
        # 兼容降级：仍设置 contextvars，供非 LangGraph 上下文直接调用工具时使用
        set_conversation_context(messages)
        set_current_user_id(user_id)
        # 组织上下文：登录用户来自 task_context.org_id（JWT），
        # 自动诊断服务身份来自 DIAGNOSIS_ORG_ID（alerts 构造 context 时注入）
        set_current_org_id((task_context or {}).get("org_id"))

        # 添加系统提示（注入记忆上下文）
        system_prompt = self._build_system_prompt(state, user_id=user_id)

        # 构建消息
        full_messages = [SystemMessage(content=system_prompt)] + messages

        # 上下文管理：按 token 预算裁剪，避免长对话超出模型上下文窗口
        full_messages = self._trim_messages_to_budget(full_messages)

        # 调用 LLM
        try:
            response = self.llm_with_tools.invoke(full_messages)
            logger.info(f"Agent Step {step_count + 1}: LLM 响应")
        except Exception as e:
            # 注意：异常文本（如 OpenAI 400 body 的 JSON）含花括号，loguru 会对消息做二次
            # format 而把 {…} 当占位符 → KeyError。故用占位符传参而非 f-string 内联。
            logger.error("LLM 调用失败: {}", e, exc_info=True)
            from ..observability.metrics import safe_increment
            safe_increment("agent_llm_fallback_total", 1, labels={"path": "call_agent"})
            response = AIMessage(content="抱歉，处理过程中出现内部错误，请稍后重试")

        # 跟踪工具调用
        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tc in response.tool_calls:
                tool_name = tc.get('name', 'unknown')
                if tool_name not in tools_used:
                    tools_used.append(tool_name)

        return {
            "messages": [response],
            "step_count": step_count + 1,
            "tools_used": tools_used,
            "retrieved_docs": retrieved_docs,
            "monitoring_evidence": monitoring_evidence,
        }

    # ========== 上下文管理：Token 预算裁剪 ==========

    def _count_message_tokens(self, msg) -> int:
        """计算单条消息的 token 数（含 4 token overhead）"""
        content = getattr(msg, "content", None)
        if content is None:
            content = str(msg)
        # content 可能是 string 或 list（多模态）
        if isinstance(content, str):
            return self.token_counter.count(content) + 4
        # 多模态消息：只统计文本部分
        total = 4
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += self.token_counter.count(part.get("text", ""))
        return total

    def _trim_messages_to_budget(self, messages: List, budget_override: Optional[int] = None) -> List:
        """按 token 预算从后往前裁剪消息，保留最近的消息。

        核心策略：
        1. SystemMessage 始终保留（不在裁剪范围内，由调用方单独处理）
        2. 从最后一条消息往前累加 token，达到预算时停止
        3. 修复裁剪边界可能破坏的 tool_calls 配对：
           - 移除开头孤立的 ToolMessage（对应的 AIMessage 已被裁掉）
           - 移除开头孤立的 AIMessage(tool_calls)（对应的 ToolMessage 已被裁掉）

        Args:
            messages: 待裁剪的消息列表（不含 SystemMessage，或 SystemMessage 已在首位）
            budget_override: 可选，临时覆盖 max_context_tokens（避免修改实例变量引发并发竞态）

        Returns:
            裁剪后的消息列表，token 总数不超过 max_context_tokens - reserved_for_output
        """
        if not messages:
            return messages

        max_ctx = budget_override if budget_override is not None else self.max_context_tokens
        budget = max_ctx - self.reserved_for_output
        if budget <= 0:
            logger.warning("上下文预算耗尽，仅保留最后一条消息")
            return self._fix_tool_calls_boundary(messages[-1:])

        # 分离 SystemMessage 和其他消息
        system_msgs = []
        other_msgs = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                system_msgs.append(msg)
            else:
                other_msgs.append(msg)

        # SystemMessage 的 token 数计入预算
        system_tokens = sum(self._count_message_tokens(m) for m in system_msgs)
        available = budget - system_tokens

        if available <= 0:
            logger.warning(
                f"System prompt 占用 {system_tokens} tokens，已耗尽预算 {budget}，"
                f"仅保留 system + 最后一条消息"
            )
            return system_msgs + self._fix_tool_calls_boundary(other_msgs[-1:])

        # 从后往前累加，保留最近的消息
        kept_reversed = []
        total = 0
        for msg in reversed(other_msgs):
            msg_tokens = self._count_message_tokens(msg)
            if total + msg_tokens > available and kept_reversed:
                # 已达预算，停止（但确保至少保留一条）
                break
            kept_reversed.append(msg)
            total += msg_tokens

        if not kept_reversed:
            # 极端情况：单条消息就超预算，保留最后一条
            kept_reversed = [other_msgs[-1]]
            total = self._count_message_tokens(other_msgs[-1])

        kept = list(reversed(kept_reversed))

        # 修复裁剪边界可能破坏的 tool_calls 配对
        kept = self._fix_tool_calls_boundary(kept)

        # 日志：裁剪情况
        original_count = len(other_msgs)
        kept_count = len(kept)
        if kept_count < original_count:
            logger.info(
                f"上下文裁剪: {original_count} → {kept_count} 条消息，"
                f"约 {total + system_tokens}/{budget} tokens"
            )

        return system_msgs + kept

    def _fix_tool_calls_boundary(self, messages: List) -> List:
        """修复裁剪边界可能破坏的 tool_calls 配对。

        裁剪后开头可能出现：
        1. 孤立的 ToolMessage：对应的 AIMessage(tool_calls) 已被裁掉 → 移除
        2. 孤立的 AIMessage(tool_calls)：对应的 ToolMessage 已被裁掉 → 移除

        Args:
            messages: 裁剪后的消息列表

        Returns:
            修复后的消息列表，tool_calls 配对完整
        """
        if not messages:
            return messages

        # 移除开头孤立的 ToolMessage（前面没有对应的 AIMessage）
        while messages and isinstance(messages[0], ToolMessage):
            messages = messages[1:]

        # 移除开头孤立的 AIMessage(tool_calls)（后面没有对应的 ToolMessage 响应）
        while messages and isinstance(messages[0], AIMessage) and getattr(messages[0], "tool_calls", None):
            # 收集这条 AIMessage 的所有 tool_call_id
            tc_ids = {tc.get("id") for tc in messages[0].tool_calls if tc.get("id")}
            # 检查接下来几条消息中是否有对应的 ToolMessage
            has_response = False
            for m in messages[1:6]:  # 最多检查接下来 5 条
                if isinstance(m, ToolMessage) and m.tool_call_id in tc_ids:
                    has_response = True
                    break
                if isinstance(m, AIMessage):
                    break  # 遇到下一条 AIMessage 就停止
            if not has_response:
                messages = messages[1:]
            else:
                break

        return messages

    @staticmethod
    def _neutralize_unpaired_tool_calls(messages) -> List:
        """中性化"未被 ToolMessage 应答"的 tool_calls AIMessage

        场景：上一轮诊断跑到 max_steps 被截断时，历史末尾可能残留一条带 tool_calls
        的 AIMessage——它的工具从未真正执行（没有对应 ToolMessage）。这样的历史若原样
        回传给 OpenAI 兼容 API，会收到 400：
          "An assistant message with 'tool_calls' must be followed by tool messages
           responding to each 'tool_call_id'"
        导致重试/续跑（同一 thread_id 恢复 checkpoint）必然失败（R5 error_storm 实测）。

        应答判定必须是"位置上紧随其后的连续 ToolMessage 块"（OpenAI 的配对规则），
        不能看全列表——同一 tool_call_id 若在前文出现过会被误判为已应答。

        处理（仅对存在未应答 tool_calls 的 AIMessage）：
        - 正文为空 → 连同其应答块一起丢弃（纯工具请求，无信息量）；
        - 有正文 → 保留正文，剥离未应答的 tool_calls，后续 ToolMessage 仍与其配对。

        说明：正常执行流中工具调用总会被 ToolNode 以 ToolMessage 应答后才进入下一轮
        _call_agent，因此本函数不会误伤正常消息；仅在"被截断/续跑"这类残缺历史上生效。
        """
        if not messages:
            return messages
        msgs = list(messages)
        out: List = []
        i = 0
        n = len(msgs)
        while i < n:
            msg = msgs[i]
            calls = (getattr(msg, "tool_calls", None) or []) \
                if isinstance(msg, AIMessage) else []
            if not calls:
                out.append(msg)
                i += 1
                continue
            # 收集紧随其后的连续 ToolMessage 块（含被忽略的应答归属）
            j = i + 1
            while j < n and isinstance(msgs[j], ToolMessage):
                j += 1
            followed_ids = {
                getattr(m, "tool_call_id", None) for m in msgs[i + 1:j]
                if getattr(m, "tool_call_id", None)
            }
            missing = [tc for tc in calls if tc.get("id") not in followed_ids]
            if not missing:
                out.append(msg)  # 全部应答，正常保留
                i += 1
                continue
            if (msg.content or "").strip():
                # 有正文：剥离未应答的 tool_calls，保留正文与已应答配对
                clone = msg.model_copy(deep=False)
                clone.tool_calls = [
                    tc for tc in calls if tc.get("id") in followed_ids]
                out.append(clone)
                i += 1
            else:
                # 空正文 + 悬空工具请求：连同其后应答块一起丢弃（避免孤儿 ToolMessage）
                i = j
        return out

    def _should_continue(self, state: AgentState) -> str:
        """决定是否继续执行

        P2 去反思节点后：诊断与 QA 共用同一套 ReAct 循环。
        - Agent 有工具调用 → 执行工具
        - Agent 无工具调用（准备回答）→ 直接结束，LLM 的回答就是最终输出
          诊断质量靠 max_steps（给足证据收集空间）+ 诊断 prompt（强制监控优先、
          至少 N 次 search_knowledge）保证，不再用独立反思节点二次判断。
        """
        messages = state["messages"]
        last_message = messages[-1]
        step_count = state.get("step_count", 0)

        # 达到最大步数
        if step_count >= self.max_steps:
            logger.info(f"达到最大步数 {self.max_steps}，结束")
            return "end"

        # 有工具调用 → 执行
        if last_message.tool_calls:
            # 记录并行工具调用
            if len(last_message.tool_calls) > 1:
                tool_names = [tc.get('name', 'unknown') for tc in last_message.tool_calls]
                logger.info(f"并行工具调用: {tool_names}")
            return "continue"

        # 无工具调用 → Agent 已给出最终回答，直接结束
        intent = state.get("intent", "unknown")
        logger.info(f"Agent 回答完成（intent={intent}），结束")
        return "end"

    @staticmethod
    def _extract_final_diagnosis(messages, intent: str = "unknown") -> str:
        """从消息历史中提取最终回答

        P2 去反思节点后：消息历史不再混入 EVIDENCE_SUFFICIENT/INSUFFICIENT 判断词，
        最后一条无 tool_calls 的 AIMessage 即为 Agent 的最终回答。

        - diagnosis: 优先取含 ### 现象 的诊断报告；找不到则取最后一条实质回答
        - qa/unknown: 直接取最后一条实质回答

        Args:
            messages: LangGraph 消息列表
            intent: 查询意图（diagnosis/qa/unknown）

        Returns:
            最终回答文本
        """
        if not messages:
            return ""

        # 诊断链路：优先找含 ### 现象 的诊断报告
        if intent == "diagnosis":
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    content = getattr(msg, "content", "") or ""
                    if "### 现象" in content:
                        return content

        # 通用：取最后一条无 tool_calls 的 AIMessage（Agent 的最终回答）
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
                return getattr(msg, "content", "") or ""

        # 兜底：最后一条消息内容
        return getattr(messages[-1], "content", "") or ""

    def _build_system_prompt(self, state: AgentState, user_id: str = None) -> str:
        """构建系统提示（根据意图选择模板，注入记忆上下文 + 证据看板）

        P1-1: 根据 state.intent 选择 QA 模板或诊断模板
        P1-2: 记忆上下文从 state.memory_context 读取（一次性组装），不再每步检索
        """
        tools_used = state.get("tools_used", [])
        retrieved_docs = state.get("retrieved_docs", [])
        monitoring_evidence = state.get("monitoring_evidence", [])
        task_context = state.get("task_context", {})
        use_web_search = task_context.get("use_web_search", False)
        intent = state.get("intent", "unknown")

        # P1-1: 根据意图选择模板
        if intent == "diagnosis":
            prompt = self._build_diagnosis_prompt()
        else:
            prompt = self._build_qa_prompt()

        # 注入证据看板（双源融合核心：让 LLM 始终看到已收集的证据全貌）
        # QA 链路通常无监控证据，看板为空时自动跳过
        evidence_summary = _build_evidence_summary(monitoring_evidence, retrieved_docs)
        if evidence_summary:
            prompt += evidence_summary + "\n"

        # P1-2: 注入记忆上下文（从 state 读取，避免每步重复检索）
        memory_context = state.get("memory_context", "")
        if memory_context:
            prompt += f"\n{memory_context}\n"

        # 根据 use_web_search 标志调整策略
        if use_web_search:
            prompt += "\n## 搜索策略\n用户启用了联网搜索，优先使用 web_search 获取最新信息。\n"

        # 添加已使用工具信息
        if tools_used:
            prompt += f"\n## 已使用工具\n{', '.join(tools_used)}\n"

        # 注入 MCP 监控工具可用性状态（仅诊断链路需要）
        if intent == "diagnosis":
            mcp_hint = self._build_mcp_status_hint()
            if mcp_hint:
                prompt += mcp_hint

        return prompt

    def _build_qa_prompt(self) -> str:
        """QA 链路系统提示（P1-1）——模板见 app/langgraph_agent/prompts.py"""
        return build_qa_prompt()

    def _build_diagnosis_prompt(self) -> str:
        """诊断链路系统提示——模板见 app/langgraph_agent/prompts.py"""
        return build_diagnosis_prompt()

    def _build_mcp_status_hint(self) -> str:
        """根据 MCP 加载状态生成系统提示片段

        监控工具不可用时，引导 Agent 走降级路径（纯知识库诊断），
        并在诊断报告中明确标注"无实时监控数据"，避免误导用户。

        判断依据：_mcp_status 字段（disabled/timeout/failed/loading 表示不可用）。
        不再按 self.tools 是否包含 query_metrics 等名字判断——因为：
        - create_tools() 始终注册内置 mock 工具（同名），MCP 成功时才被真实工具替换；
        - 按名字判断会始终为 True，导致降级提示永远不注入，失去保护意义。

        Returns:
            提示片段（空字符串表示监控工具可用，无需注入）
        """
        if self._mcp_status == "success":
            return ""

        # 监控工具不可用的几种情况
        if self._mcp_status == "disabled":
            reason = "监控工具未启用（MCP_ENABLED=False）"
        elif self._mcp_status == "timeout":
            reason = f"监控工具加载超时（{self._mcp_last_error}）"
        elif self._mcp_status == "failed":
            reason = f"监控工具加载失败（{self._mcp_last_error}）"
        else:
            reason = f"监控工具不可用（状态: {self._mcp_status}）"

        return f"""
## ⚠️ 监控工具降级提示
**当前实时监控不可用**：{reason}

### 降级诊断流程
因无法获取实时监控数据，请按以下调整诊断流程：
1. **跳过阶段 2A（实时监控取证）**，直接进入阶段 2B（知识库检索）
2. 诊断报告"证据"部分必须明确标注"**无实时监控数据**（监控工具不可用）"
3. 置信度最高只能给"**中**"（缺乏实时证据，仅基于历史经验）
4. 处置方案中应建议用户"补充实时监控数据后重新诊断"以获得高置信度结论

### 禁止行为
- 不要调用 query_metrics / query_logs（当前仅有 mock 数据，不可信，会误导诊断）
- 不要在报告中假装有监控数据
"""

    def _get_sufficiency_for_run(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """从 run 的结果组件计算证据充分度（含 MCP 降级判定）"""
        mcp_status = getattr(self, "_mcp_status", "ok")
        degraded = mcp_status in ("failed", "timeout")
        return _compute_evidence_sufficiency(
            monitoring_evidence=result.get("monitoring_evidence"),
            citations=result.get("citations"),
            tools_used=result.get("tools_used"),
            diagnosis_report=result.get("diagnosis_report"),
            mcp_degraded=degraded,
        )

    async def _write_tool_audit_log(self, messages, user_id, session_id, intent):
        """将本次 run 的所有工具调用写入审计日志（Mongo tool_audit_logs）

        审计动机：Agent 会把监控数据/知识库内容发给外部 LLM API，且可能执行
        有副作用的操作（创建工单）。企业安全评审要求这些动作可追溯——
        谁在什么会话里、以什么意图、调用了什么工具、传了什么参数。

        从消息历史提取（AIMessage.tool_calls 与 ToolMessage 一一对应），
        参数截断到 200 字符防止日志膨胀。
        """
        import uuid as _uuid
        from datetime import datetime as _dt

        audit_entries = []
        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            for tc in (getattr(msg, 'tool_calls', None) or []):
                args = tc.get('args') or {}
                args_str = str(args)
                audit_entries.append({
                    "audit_id": f"audit_{_uuid.uuid4().hex[:12]}",
                    "tool_name": tc.get('name', 'unknown'),
                    "tool_args": args_str[:200],
                    "user_id": user_id or "",
                    "session_id": session_id or "",
                    "intent": intent,
                })
        if not audit_entries:
            return

        now_str = _dt.now().isoformat()
        from ..core.database import db
        for entry in audit_entries:
            entry["created_at"] = now_str
            await db.save_tool_audit_log(entry)
        logger.debug(f"工具审计日志已写入: {len(audit_entries)} 条")

    async def run(self, user_input: str, session_id: str = None, context: Dict = None, use_web_search: bool = False) -> Dict:
        """
        运行 Agent

        Args:
            user_input: 用户输入
            session_id: 会话 ID（用于 checkpoint 恢复历史）
            context: 额外上下文
            use_web_search: 是否使用 Web Search

        Returns:
            Dict: 执行结果
        """
        # 确保 SQLite 持久化已初始化（延迟异步初始化）
        await self._ensure_saver()

        # 构建 checkpoint 配置
        config = {"configurable": {"thread_id": session_id or "default"}}

        # 将 use_web_search 传递到 context 中
        if context is None:
            context = {}
        context["use_web_search"] = use_web_search

        # 构建初始状态（checkpoint 会自动恢复历史消息）
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "tools_used": [],
            "tool_results": {},
            "task_type": "",
            "task_context": context or {},
            "intent": "unknown",  # P1-1: 由 _route_intent 节点设置
            "memory_context": "",  # P1-2: 由 _route_intent 节点一次性组装
            "retrieved_docs": [],
            "citations": [],
            "monitoring_evidence": [],
            "diagnosis_report": None,
            "step_count": 0,
            "max_steps": self.max_steps,
        }

        try:
            # 执行图（传入 config 以使用 checkpoint）
            final_state = await self.graph.ainvoke(initial_state, config=config)

            # 提取结果
            messages = final_state["messages"]
            intent = final_state.get("intent", "unknown")

            # P0: 基于 intent 提取最终回答（QA 链路直接取 Agent 回答，诊断链路找诊断报告）
            final_content = self._extract_final_diagnosis(messages, intent=intent)

            # 解析结构化诊断报告（仅诊断链路有，QA 链路返回 None）
            diagnosis_report = _parse_diagnosis_report(final_content) if intent == "diagnosis" else None

            result = {
                "content": final_content,
                "tools_used": final_state.get("tools_used", []),
                "citations": _build_citations(final_state.get("retrieved_docs", [])),
                "monitoring_evidence": final_state.get("monitoring_evidence", []),
                "diagnosis_report": diagnosis_report,
                "step_count": final_state.get("step_count", 0),
                "prompt_version": PROMPT_VERSION,
            }

            # 证据充分度（规则计算，校准 LLM 自报置信度）——仅诊断链路有意义
            if intent == "diagnosis":
                result["evidence_sufficiency"] = self._get_sufficiency_for_run(result)

            # 工具调用审计：记录本次 run 的所有工具调用（名称+参数+调用者），
            # 满足"哪些数据发给了外部 LLM"的可追溯要求。审计失败不影响诊断主链路。
            try:
                await self._write_tool_audit_log(
                    messages=messages,
                    user_id=(context or {}).get("user_id"),
                    session_id=session_id,
                    intent=intent,
                )
            except Exception as audit_err:
                logger.warning(f"工具审计日志写入失败（不影响诊断）: {audit_err}")

            return result

        except Exception as e:
            # 异常文本/堆栈可能含花括号，loguru 二次 format 会把 {…} 当占位符致 KeyError，
            # 必须用占位符传参，且勿把日志失败带崩诊断返回。
            logger.error("Agent 执行失败: {}", e, exc_info=True)
            from ..observability.metrics import safe_increment
            safe_increment("agent_llm_fallback_total", 1, labels={"path": "run"})
            return {
                "content": "抱歉，处理过程中出现内部错误，请稍后重试",
                "tools_used": [],
                "citations": [],
                "monitoring_evidence": [],
                "diagnosis_report": None,
                "step_count": 0,
                "prompt_version": PROMPT_VERSION,
            }

    async def run_stream(self, user_input: str, session_id: str = None, context: Dict = None, use_web_search: bool = False):
        """
        流式运行 Agent（token 级流式，P1-1 优化）

        使用 LangGraph astream_events(v2) 捕获 LLM 的 token 流，
        实现"逐字输出"的真实流式体验，同时保留工具调用事件。

        事件类型：
        - {"type": "start"}                      开始
        - {"type": "tool_calls", "tools": [...]} 工具调用开始
        - {"type": "tool_result", "name": ..., "content": ...} 工具结果
        - {"type": "token", "content": "..."}    LLM token 流（核心）
        - {"type": "done", "tools_used": [...], "step_count": N} 完成

        Args:
            user_input: 用户输入
            session_id: 会话 ID
            context: 额外上下文
            use_web_search: 是否使用 Web Search

        Yields:
            Dict: 流式事件
        """
        # 确保 SQLite 持久化已初始化（延迟异步初始化）
        await self._ensure_saver()

        # 构建 checkpoint 配置
        config = {"configurable": {"thread_id": session_id or "default"}}

        # 将 use_web_search 传递到 context 中
        if context is None:
            context = {}
        context["use_web_search"] = use_web_search

        # 构建初始状态
        initial_state = {
            "messages": [HumanMessage(content=user_input)],
            "tools_used": [],
            "tool_results": {},
            "task_type": "",
            "task_context": context or {},
            "intent": "unknown",  # P1-1: 由 _route_intent 节点设置
            "memory_context": "",  # P1-2: 由 _route_intent 节点一次性组装
            "retrieved_docs": [],
            "citations": [],
            "monitoring_evidence": [],
            "diagnosis_report": None,
            "step_count": 0,
            "max_steps": self.max_steps,
        }

        # P1-1: token 级流式
        # 跟踪当前是否在最终回答阶段（无 tool_calls 的 AIMessage）
        # astream_events 会按顺序触发：
        #   on_chat_model_start (LLM 调用开始)
        #     on_chat_model_stream (token 流)  <-- 我们要的
        #   on_chat_model_end (LLM 调用结束)
        #   on_tool_start (工具调用开始)
        #   on_tool_end (工具返回)
        # 最后通过 aget_state 获取最终状态
        final_state_values = None

        try:
            # 用 astream_events 捕获 LLM token
            seen_tools_in_step = set()  # 当前 step 已发过 tool_calls 事件的工具
            current_llm_content = ""    # 当前 LLM 调用的累积内容（用于补发判断）

            async for event in self.graph.astream_events(
                initial_state,
                config=config,
                version="v2",
            ):
                kind = event.get("event")
                name = event.get("name", "")
                data = event.get("data", {})

                # 1. LLM token 流（核心）
                if kind == "on_chat_model_stream":
                    chunk = data.get("chunk")
                    if chunk is None:
                        continue

                    # 提取 token 内容
                    token_text = ""
                    if hasattr(chunk, "content") and chunk.content:
                        token_text = chunk.content

                    if token_text:
                        current_llm_content += token_text
                        yield {"type": "token", "content": token_text}

                    # 工具调用 chunk（累积中，等 on_tool_start 统一发送）
                    # 不在这里 yield tool_calls，避免重复

                # 2. 工具调用开始
                elif kind == "on_tool_start":
                    tool_name = name
                    if tool_name and tool_name not in seen_tools_in_step:
                        seen_tools_in_step.add(tool_name)
                        yield {
                            "type": "tool_calls",
                            "tools": list(seen_tools_in_step),
                        }

                # 3. 工具调用结束
                elif kind == "on_tool_end":
                    output = data.get("output")
                    tool_content = ""
                    if output is not None:
                        if hasattr(output, "content"):
                            tool_content = output.content or ""
                        elif isinstance(output, str):
                            tool_content = output
                        else:
                            tool_content = str(output)

                    # 截断长工具结果
                    if len(tool_content) > 200:
                        tool_content = tool_content[:200] + "..."

                    yield {
                        "type": "tool_result",
                        "name": name,
                        "content": tool_content,
                    }
                    # 重置 seen_tools 进入下一轮
                    seen_tools_in_step.clear()

            # 获取最终状态
            final_state_values = await self.graph.aget_state(config)
            if final_state_values and final_state_values.values:
                values = final_state_values.values
                messages = values.get("messages", [])
                tools_used = values.get("tools_used", [])
                step_count = values.get("step_count", 0)

                # 补发逻辑：P2 去反思后无降级报告，Agent 的最终回答已通过 token 流发送。
                # 仅当 token 流完全为空（LLM 未走 stream，如异常降级路径）时补发最终回答。
                if messages and not current_llm_content:
                    last_msg = messages[-1]
                    last_content = getattr(last_msg, "content", "") or ""
                    if last_content:
                        yield {"type": "token", "content": last_content}
                        current_llm_content = last_content

                # P0: 基于 intent 解析诊断报告（QA 链路无诊断报告）
                diagnosis_report = None
                intent = values.get("intent", "unknown")
                if messages and intent == "diagnosis":
                    final_content = self._extract_final_diagnosis(messages, intent=intent)
                    if final_content:
                        diagnosis_report = _parse_diagnosis_report(final_content)

                yield {
                    "type": "done",
                    "tools_used": tools_used,
                    "step_count": step_count,
                    "citations": _build_citations(values.get("retrieved_docs", [])),
                    "monitoring_evidence": values.get("monitoring_evidence", []),
                    "diagnosis_report": diagnosis_report,
                    "prompt_version": PROMPT_VERSION,
                }

        except Exception as e:
            logger.error("流式执行失败: {}", e, exc_info=True)
            from ..observability.metrics import safe_increment
            safe_increment("agent_llm_fallback_total", 1, labels={"path": "run_stream"})
            yield {
                "type": "error",
                "content": "抱歉，处理过程中出现内部错误，请稍后重试",
            }

    def run_sync(self, user_input: str, session_id: str = None, context: Dict = None) -> Dict:
        """
        同步运行 Agent

        Args:
            user_input: 用户输入
            session_id: 会话 ID
            context: 额外上下文

        Returns:
            Dict: 执行结果
        """
        import asyncio
        return asyncio.run(self.run(user_input, session_id, context))

    def get_graph(self):
        """获取图（用于可视化）"""
        return self.graph
