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

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    HAS_ASYNC_SQLITE = True
except ImportError:
    HAS_ASYNC_SQLITE = False
    logger.warning("langgraph-checkpoint-sqlite 未安装，无法使用 SQLite 持久化")

from ..core.config import settings
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

        # 绑定工具到 LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

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
                self._mcp_status = "timeout"
                self._mcp_last_error = f"MCP 加载超时（>{mcp_timeout}s）"
                logger.error(
                    f"MCP 工具加载超时（>{mcp_timeout}s），降级为纯知识库模式。"
                    f"可能原因：server 子进程启动慢、stdio 管道阻塞、server 脚本异常"
                )
                # 尝试关闭半连接的 client（best effort，失败忽略）
                await self._safe_close_mcp_client(client)
                return 0

            if not mcp_tools:
                self._mcp_status = "failed"
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
            self._mcp_status = "success"
            self._mcp_last_error = None

            tool_names = [t.name for t in mcp_tools]
            logger.info(
                f"MCP 工具加载完成: {len(mcp_tools)} 个 - {tool_names}，"
                f"Agent 工具总数: {len(self.tools)}"
            )
            return len(mcp_tools)

        except Exception as e:
            self._mcp_status = "failed"
            self._mcp_last_error = str(e)
            logger.error(f"MCP 工具加载失败（Agent 将仅使用原有工具）: {e}")
            return 0

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
            logger.debug(f"关闭 MCP client 失败（忽略）: {e}")

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
                logger.warning(f"MongoDB checkpointer 初始化失败，降级为 MemorySaver: {e}")
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
        """一次性组装记忆上下文（用户画像 + 历史 + 档案记忆）

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

            # 用户画像
            profile = memory.core_memory.get_user_profile(user_id)
            profile_context = profile.to_context_string()
            if profile_context:
                parts.append(f"## 用户信息\n{profile_context}")

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
            logger.debug(f"组装记忆上下文失败: {e}")
            return ""

    def _call_agent(self, state: AgentState) -> Dict:
        """调用 Agent（LLM）"""
        messages = state["messages"]
        step_count = state.get("step_count", 0)
        tools_used = state.get("tools_used", [])
        task_context = state.get("task_context", {})
        user_id = task_context.get("user_id")

        # 引用溯源：收集检索结果（两个来源合并去重）
        # 1. 模块级 buffer（主要方案：search_knowledge 工具写入）
        buffered_docs = pop_retrieval_buffer()
        # 2. ToolMessage artifact（备用方案：未来 LangGraph 版本可能支持）
        artifact_docs = self._collect_retrieved_docs(messages)
        # 合并已有 + 新增，按 doc_id + content 去重
        existing = state.get("retrieved_docs", [])
        retrieved_docs = self._merge_retrieved_docs(existing + buffered_docs + artifact_docs)

        # 双源融合：提取监控证据（query_metrics/query_logs 返回的结构化数据）
        # 与知识库证据（retrieved_docs）对称管理，供证据看板注入 prompt
        existing_monitoring = state.get("monitoring_evidence", [])
        new_monitoring = self._extract_monitoring_evidence(messages)
        monitoring_evidence = self._merge_monitoring_evidence(existing_monitoring + new_monitoring)

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
            logger.error(f"LLM 调用失败: {e}", exc_info=True)
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

    @staticmethod
    def _collect_retrieved_docs(messages) -> List[Dict]:
        """从 messages 中的 ToolMessage 提取 artifact（备用方案）

        当前 LangGraph 1.1.x 的 ToolNode 调用 tool.invoke()，
        对 response_format="content_and_artifact" 只返回 content，artifact 丢失。
        此方法作为未来兼容的备用方案，主要数据来源是 pop_retrieval_buffer()。
        """
        retrieved: List[Dict] = []
        seen_ids: set = set()
        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue
            tc_id = getattr(msg, 'tool_call_id', None)
            if tc_id and tc_id in seen_ids:
                continue
            artifact = getattr(msg, 'artifact', None)
            if artifact and isinstance(artifact, list):
                if tc_id:
                    seen_ids.add(tc_id)
                retrieved.extend(artifact)
        return retrieved

    @staticmethod
    def _merge_retrieved_docs(docs: List[Dict]) -> List[Dict]:
        """合并检索结果并去重（按 doc_id + content 前 100 字符）"""
        seen: set = set()
        unique: List[Dict] = []
        for doc in docs:
            key = (doc.get("doc_id", ""), doc.get("content", "")[:100])
            if key in seen:
                continue
            seen.add(key)
            unique.append(doc)
        return unique

    @staticmethod
    def _extract_monitoring_evidence(messages) -> List[Dict]:
        """从 messages 中的 ToolMessage 提取监控证据（query_metrics/query_logs 返回）

        与 _collect_retrieved_docs 类似，但专门处理 MCP 监控工具的结构化 JSON 数据。
        将 JSON 返回值解析为 {type, service, summary, details} 格式，供证据看板使用。

        为什么需要这个：监控工具返回的 JSON 散落在 ToolMessage 文本中，
        LLM 无法可靠回顾"我查了什么指标、值是多少"。结构化提取后注入到
        system prompt 的证据看板，LLM 始终能看到证据全貌。

        Returns:
            [{"type":"metrics"/"logs", "service":"...", "summary":"...", "details":{...}}]
        """
        import json

        # 1. 构建 tool_call_id → tool_name 映射（从 AIMessage.tool_calls）
        tool_call_names: Dict[str, str] = {}
        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls'):
                for tc in (msg.tool_calls or []):
                    tc_id = tc.get('id')
                    tc_name = tc.get('name')
                    if tc_id and tc_name:
                        tool_call_names[tc_id] = tc_name

        # 2. 遍历 ToolMessage，提取监控工具结果
        evidence: List[Dict] = []
        seen_call_ids: set = set()

        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue
            tc_id = getattr(msg, 'tool_call_id', None)
            if tc_id and tc_id in seen_call_ids:
                continue

            # 工具名：优先从映射查（兼容旧版本 ToolMessage 无 name 字段）
            tool_name = tool_call_names.get(tc_id, '') or getattr(msg, 'name', '')
            # 兼容旧 mock 工具名 + 新 MCP 工具名：
            # 旧 mock: query_metrics / query_logs / analyze_chart
            # 新 MCP:  query_prometheus / query_prometheus_range / query_system_overview
            #          query_logs(Loki) / query_loki / list_containers
            #          query_alerts / query_alertmanager / query_silences
            _MONITORING_TOOL_NAMES = {
                'query_metrics', 'query_logs', 'analyze_chart',
                'get_recent_changes',
                'query_prometheus', 'query_prometheus_range', 'query_system_overview',
                'query_loki', 'list_containers',
                'query_alerts', 'query_alertmanager', 'query_silences',
            }
            if tool_name not in _MONITORING_TOOL_NAMES:
                continue

            if tc_id:
                seen_call_ids.add(tc_id)

            # 解析 JSON 内容
            # MCP 工具（langchain-mcp-adapters）返回 content 为 list[TextBlock] 格式：
            # [{'type': 'text', 'text': '{"service":"...",...}'}]
            # 普通工具返回 content 为 str
            raw_content = msg.content
            if isinstance(raw_content, list):
                # MCP TextBlock 格式：提取所有 text 块拼接
                text_parts = [block.get('text', '') for block in raw_content if isinstance(block, dict) and block.get('type') == 'text']
                raw_content = '\n'.join(text_parts)
            try:
                data = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, dict) or data.get("error"):
                continue

            # 分发到对应解析器：
            # - 指标类（query_metrics / query_prometheus / query_system_overview）→ metrics 证据
            # - 日志类（query_logs mock / query_logs Loki / query_loki）→ logs 证据
            # - 告警类（query_alerts / query_alertmanager）→ alerts 证据
            # - 图表类（analyze_chart）→ chart 证据
            # - list_containers / query_silences → 跳过（辅助信息，不是诊断证据）
            if tool_name in ('query_metrics', 'query_prometheus',
                             'query_prometheus_range', 'query_system_overview'):
                ev = LangGraphAgent._parse_metrics_evidence(data)
            elif tool_name == 'analyze_chart':
                ev = LangGraphAgent._parse_chart_evidence(data)
            elif tool_name in ('query_logs', 'query_loki'):
                ev = LangGraphAgent._parse_logs_evidence(data)
            elif tool_name in ('query_alerts', 'query_alertmanager'):
                ev = LangGraphAgent._parse_alerts_evidence(data)
            elif tool_name == 'get_recent_changes':
                ev = LangGraphAgent._parse_changes_evidence(data)
            else:
                ev = None  # list_containers / query_silences 等辅助工具，不提取为证据
            if ev:
                evidence.append(ev)

        return evidence

    @staticmethod
    def _merge_monitoring_evidence(evidence: List[Dict]) -> List[Dict]:
        """合并监控证据并去重（按 type + service + summary 前 80 字符）"""
        seen: set = set()
        unique: List[Dict] = []
        for ev in evidence:
            key = (ev.get("type", ""), ev.get("service", ""), ev.get("summary", "")[:80])
            if key in seen:
                continue
            seen.add(key)
            unique.append(ev)
        return unique

    @staticmethod
    def _parse_metrics_evidence(data: Dict) -> Optional[Dict]:
        """解析监控指标工具返回的 JSON 为结构化监控证据

        兼容 5 种返回格式：
        1. 旧 mock query_metrics(all): {"service":"...", "metrics": {name: {value, baseline, unit}}}
        2. 旧 mock query_metrics(单): {"service":"...", "metric":"...", "value":..., "baseline":...}
        3. 新 MCP query_system_overview: {"timestamp":"...", "metrics": {name: {value, labels}}}
        4. 新 MCP query_prometheus: {"query":"...", "count":N, "values":[{labels, value}]}
        5. 新 MCP query_prometheus_range: {"query":"...", "series":[{labels, points}]}
        """

        def _fmt_val(v) -> str:
            """格式化数值：浮点保留 2 位，百分比类自动 *100"""
            if isinstance(v, (int, float)):
                return f"{v:.2f}" if abs(v) < 1e6 else f"{v:.2e}"
            return str(v)

        # 格式3: 新 query_system_overview（有 metrics，无 service/baseline）
        if "metrics" in data and "service" not in data:
            parts = []
            details = {}
            for name, m in data["metrics"].items():
                if not isinstance(m, dict) or "error" in m:
                    continue
                if "value" in m and m["value"] is not None:
                    v = m["value"]
                    parts.append(f"{name}={_fmt_val(v)}")
                    details[name] = {"value": v}
                elif "values" in m:
                    # 多值指标（如各网卡流量），取汇总值
                    vals = m["values"]
                    total = sum(
                        v.get("value", 0) for v in vals
                        if isinstance(v, dict) and isinstance(v.get("value"), (int, float))
                    )
                    parts.append(f"{name}(sum)={_fmt_val(total)}")
                    details[name] = {"count": len(vals), "total": total}
            return {
                "type": "metrics",
                "service": "system",
                "summary": ", ".join(parts) if parts else "无指标数据",
                "details": details,
            }

        # 格式1: 旧 mock query_metrics(all)（有 service + metrics）
        if "metrics" in data:
            service = data.get("service", "unknown")
            parts = []
            details = {}
            for metric_name, metric_data in data["metrics"].items():
                if not isinstance(metric_data, dict):
                    continue
                value = metric_data.get("value")
                baseline = metric_data.get("baseline")
                unit = metric_data.get("unit", "")

                if unit == "ratio" and isinstance(value, (int, float)):
                    val_str = f"{value * 100:.0f}%"
                    base_str = f"{baseline * 100:.1f}%" if isinstance(baseline, (int, float)) else "N/A"
                    parts.append(f"{metric_name}={val_str}(基线{base_str})")
                elif isinstance(baseline, (int, float)) and isinstance(value, (int, float)):
                    parts.append(f"{metric_name}={value}(基线{baseline})")
                else:
                    parts.append(f"{metric_name}={value}")

                details[metric_name] = {
                    "value": value, "baseline": baseline, "unit": unit,
                    "description": metric_data.get("description", ""),
                }
            return {
                "type": "metrics",
                "service": service,
                "summary": ", ".join(parts) if parts else "无指标数据",
                "details": details,
            }

        # 格式4: 新 query_prometheus（有 values 列表）
        if "values" in data:
            query = data.get("query", "unknown")
            values = data.get("values", [])
            parts = []
            details = {}
            for v in values[:5]:
                if not isinstance(v, dict):
                    continue
                labels = v.get("labels", {})
                val = v.get("value")
                name = labels.get("__name__") or labels.get("device") or query[:30]
                parts.append(f"{name}={_fmt_val(val)}")
                details[name] = {"value": val, "labels": labels}
            return {
                "type": "metrics",
                "service": "prometheus",
                "summary": ", ".join(parts) if parts else f"查询: {query[:40]}（无数据）",
                "details": details,
            }

        # 格式5: 新 query_prometheus_range（有 series 时序数据）
        if "series" in data:
            query = data.get("query", "unknown")
            series = data.get("series", [])
            parts = []
            details = {}
            for s in series[:3]:
                if not isinstance(s, dict):
                    continue
                labels = s.get("labels", {})
                points = s.get("points", [])
                name = labels.get("__name__") or query[:30]
                if points:
                    last_val = points[-1].get("value", 0)
                    parts.append(f"{name}[末值]={_fmt_val(last_val)}")
                    details[name] = {"last_value": last_val, "points": len(points)}
            return {
                "type": "metrics",
                "service": "prometheus_range",
                "summary": ", ".join(parts) if parts else f"查询: {query[:40]}（无数据）",
                "details": details,
            }

        # 格式2: 旧 mock 单指标（fallback）
        service = data.get("service", "unknown")
        metric_name = data.get("metric", "unknown")
        value = data.get("value")
        baseline = data.get("baseline")
        unit = data.get("unit", "")
        if unit == "ratio" and isinstance(value, (int, float)):
            val_str = f"{value * 100:.0f}%"
            base_str = f"{baseline * 100:.1f}%" if isinstance(baseline, (int, float)) else "N/A"
            summary = f"{metric_name}={val_str}(基线{base_str})"
        else:
            summary = f"{metric_name}={value}"
        return {
            "type": "metrics",
            "service": service,
            "summary": summary,
            "details": {metric_name: {"value": value, "baseline": baseline, "unit": unit}},
        }

    @staticmethod
    def _parse_logs_evidence(data: Dict) -> Optional[Dict]:
        """解析 query_logs 返回的 JSON 为结构化监控证据

        兼容两种返回格式：
        1. 旧 mock 格式：{"service":"...", "keyword":"...", "count":N, "logs":["text", ...]}
        2. 新 Loki 格式：{"query":"...", "range_minutes":N, "count":N,
                          "logs":[{"timestamp","container","line","labels"}, ...]}
        """
        # service：旧 mock 有 service 字段；Loki 用 container（取第一条日志的 container）
        service = data.get("service") or data.get("container") or "unknown"
        # keyword：旧 mock 有 keyword；Loki 用 query（LogQL）
        keyword = data.get("keyword") or data.get("query") or "unknown"
        logs = data.get("logs", [])
        count = data.get("count", len(logs))

        # 提取前 2 条日志的关键内容（截断避免过长）
        # 兼容字符串（旧 mock）和 dict（新 Loki）两种元素格式
        sample_logs = []
        for line in logs[:2]:
            if isinstance(line, dict):
                # Loki 格式：取 line 字段（真实日志文本）
                text = line.get("line", "")
                ts = line.get("timestamp", "")
                container = line.get("container", "")
                text = f"[{ts}]{container}: {text}" if ts else text
            elif isinstance(line, str):
                text = line
            else:
                text = str(line)
            sample_logs.append(text[:120])

        summary = f"{keyword}: {count}条日志"
        if sample_logs:
            summary += f", 关键: {' | '.join(sample_logs)}"

        return {
            "type": "logs",
            "service": service,
            "summary": summary,
            "details": {"keyword": keyword, "count": count, "sample_logs": logs[:5]},
        }

    @staticmethod
    def _parse_chart_evidence(data: Dict) -> Optional[Dict]:
        """解析 analyze_chart 返回的 JSON 为结构化监控证据（VLM 看图结果）

        格式: {"service":"...", "chart_type":"...", "metrics": {...},
               "anomalies": [...], "insights": "..."}
        """
        service = data.get("service", "unknown")
        chart_type = data.get("chart_type", "overview")

        # 指标部分（复用 metrics 解析逻辑，支持 ratio 转百分比）
        metric_parts = []
        metrics = data.get("metrics", {})
        for name, m in metrics.items():
            if not isinstance(m, dict):
                continue
            value = m.get("value")
            baseline = m.get("baseline")
            if m.get("unit") == "ratio" and isinstance(value, (int, float)):
                base_str = f"{baseline * 100:.1f}%" if isinstance(baseline, (int, float)) else "N/A"
                metric_parts.append(f"{name}={value * 100:.0f}%(基线{base_str})")
            else:
                metric_parts.append(f"{name}={value}")
        metrics_str = ", ".join(metric_parts) if metric_parts else "无指标"

        # 异常模式部分（VLM 看图才能发现的形态级信息）
        anomalies = data.get("anomalies", [])
        anomaly_str = "; ".join(a.get("description", "") for a in anomalies[:3]) if anomalies else "无明显异常"

        # 洞察（跨指标关联推理）
        insights = data.get("insights", "")

        summary = f"图表分析({chart_type}): {metrics_str}"
        if anomalies:
            summary += f" | 异常: {anomaly_str}"
        if insights:
            summary += f" | 洞察: {insights[:100]}"

        return {
            "type": "chart",
            "service": service,
            "summary": summary,
            "details": {
                "chart_type": chart_type,
                "metrics": metrics,
                "anomalies": anomalies,
                "insights": insights,
            },
        }

    @staticmethod
    def _parse_alerts_evidence(data: Dict) -> Optional[Dict]:
        """解析 alertmanager 工具返回的 JSON 为结构化监控证据

        兼容两种返回格式：
        1. query_alerts: {"count":N, "alerts":[{alert_name,severity,summary,...}]}
        2. query_alertmanager: {"endpoint":"alerts", "data":[原始告警列表]}
        """
        # query_alertmanager 格式：data 字段是原始 API 响应列表
        if "data" in data and isinstance(data["data"], list):
            alerts_raw = data["data"]
            alerts = []
            for a in alerts_raw:
                labels = a.get("labels", {}) if isinstance(a, dict) else {}
                annotations = a.get("annotations", {}) if isinstance(a, dict) else {}
                status = a.get("status", {}) if isinstance(a, dict) else {}
                alerts.append({
                    "alert_name": labels.get("alertname", "unknown"),
                    "severity": labels.get("severity", "unknown"),
                    "state": status.get("state", "unknown"),
                    "summary": annotations.get("summary", ""),
                })
        else:
            # query_alerts 格式：alerts 字段已格式化
            alerts = data.get("alerts", [])

        count = data.get("count", len(alerts))

        # 按严重级别统计 + 提取告警名
        severity_count = {"critical": 0, "warning": 0, "info": 0}
        alert_names = []
        for a in alerts:
            sev = a.get("severity", "unknown")
            severity_count[sev] = severity_count.get(sev, 0) + 1
            name = a.get("alert_name") or a.get("alertname") or "unknown"
            alert_names.append(name)

        # summary：告警数 + 严重级别分布 + 告警名
        parts = [f"{count}条告警"]
        for sev, n in severity_count.items():
            if n > 0:
                parts.append(f"{sev}:{n}")
        if alert_names:
            parts.append("[" + ", ".join(alert_names[:3]) + "]")
        summary = " ".join(parts)

        return {
            "type": "alerts",
            "service": "alertmanager",
            "summary": summary,
            "details": {
                "count": count,
                "severity_count": severity_count,
                "alerts": alerts[:5],  # 保留前 5 条详情
            },
        }

    @staticmethod
    def _parse_changes_evidence(data: Dict) -> Optional[Dict]:
        """解析 get_recent_changes 返回的 JSON 为变更事件证据

        返回格式: {"service":"...", "hours":N, "count":N, "changes":[{change_id,type,time,description}]}
        变更是生产故障的第一大根因，单独作为一类证据（type="changes"）进入证据看板。
        """
        changes = data.get("changes", [])
        if not changes:
            return {
                "type": "changes",
                "service": data.get("service", "unknown"),
                "summary": f"最近 {data.get('hours', 24)}h 无变更事件",
                "details": {"count": 0},
            }

        type_mark = {
            "deploy": "发版", "config_change": "配置变更",
            "scale": "扩缩容", "infra": "基础设施",
        }
        parts = []
        for c in changes[:3]:
            t = type_mark.get(c.get("type", ""), c.get("type", "变更"))
            parts.append(f"[{t}] {c.get('time', '?')} {c.get('description', '')[:60]}")

        return {
            "type": "changes",
            "service": data.get("service", "unknown"),
            "summary": f"最近 {data.get('hours', 24)}h {len(changes)} 条变更: " + "；".join(parts),
            "details": {"count": len(changes), "changes": changes[:5]},
        }

    @staticmethod
    def _build_citations(retrieved_docs: List[Dict]) -> List[Dict]:
        """从检索结果构建引用列表（去重 + 按分数排序 + 重新编号）

        Args:
            retrieved_docs: 从 ToolMessage artifact 收集的检索结果

        Returns:
            清理后的引用列表，每项包含 index/doc_id/title/heading_path/score/source/image_path
        """
        if not retrieved_docs:
            return []

        # 按 doc_id + content 前 100 字符去重（多次 search_knowledge 调用可能有重复）
        # 保留 LLM 可见的原始 index（工具返回时已编号），不做 sort+reindex，
        # 否则 LLM 文本中的 [1] 与最终引用列表的 [1] 会错位。
        seen: set = set()
        citations = []
        for doc in retrieved_docs:
            dedup_key = (doc.get("doc_id", ""), doc.get("content", "")[:100])
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            citations.append({
                "index": doc.get("index", 0),
                "doc_id": doc.get("doc_id", ""),
                "title": doc.get("title", ""),
                "heading_path": doc.get("heading_path", ""),
                "score": doc.get("score", 0),
                "source": doc.get("source", "knowledge_base"),
                "image_path": doc.get("image_path"),
                "doc_type": doc.get("doc_type", ""),
                "service": doc.get("service", ""),
                # 知识时效：检索层对过期文档打了 _expired 标记（valid_until 已过）
                "expired": bool((doc.get("metadata") or {}).get("_expired")),
            })
        return citations

    @staticmethod
    def _parse_diagnosis_report(content: str) -> Optional[Dict]:
        """从 LLM 的 Markdown 输出中解析结构化诊断报告

        按 ### 标题提取各段：现象/证据/根因分析/处置方案/置信度。
        鲁棒性设计：解析失败返回 None，调用方降级为纯文本展示。

        Returns:
            {
                "symptom": "...",          # 现象
                "evidence": "...",         # 证据
                "root_cause": "...",       # 根因分析
                "solution": "...",         # 处置方案
                "confidence": "...",       # 置信度原文
                "confidence_level": "high" # high/medium/low（解析失败为 unknown）
            } 或 None（无 root_cause 且无 solution 时视为非诊断回答）
        """
        import re

        if not content:
            return None

        # 匹配 ### 标题 + 内容（直到下一个 ### 或文末）
        pattern = r'###\s+(.+?)\s*\n(.*?)(?=\n###\s+|\Z)'
        sections: Dict[str, str] = {}
        for match in re.finditer(pattern, content, re.DOTALL):
            title = match.group(1).strip()
            body = match.group(2).strip()
            sections[title] = body

        # 中文标题 → 英文字段映射
        field_map = {
            "现象": "symptom",
            "证据": "evidence",
            "根因分析": "root_cause",
            "根因": "root_cause",
            "处置方案": "solution",
            "解决方案": "solution",
            "置信度": "confidence",
        }
        report: Dict[str, str] = {}
        for cn_title, en_field in field_map.items():
            if cn_title in sections and en_field not in report:
                report[en_field] = sections[cn_title]

        # 至少要有 root_cause 或 solution 才算有效诊断报告
        if not report.get("root_cause") and not report.get("solution"):
            return None

        # 解析置信度等级（取置信度文本前 10 字符判断）
        conf_text = report.get("confidence", "")
        conf_level = "unknown"
        if conf_text:
            head = conf_text[:10]
            if "高" in head:
                conf_level = "high"
            elif "中" in head:
                conf_level = "medium"
            elif "低" in head:
                conf_level = "low"
        report["confidence_level"] = conf_level

        return report

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

    @staticmethod
    def _build_evidence_summary(monitoring_evidence: List[Dict], retrieved_docs: List[Dict]) -> str:
        """构建证据看板：聚合监控证据 + 知识库证据，标注完整性

        这是双源融合推理的核心——将两类证据结构化呈现在一个看板中，
        让 LLM 在每一步都能看到"已收集了什么、还缺什么"，而不是靠记忆对话历史。

        为什么不用 LLM 自己回忆：工具结果散落在 ToolMessage 文本中，
        LLM 无法可靠回顾"我查了什么"，结构化看板直接注入 prompt 解决这个问题。

        Returns:
            证据看板文本（注入 system prompt）
        """
        if not monitoring_evidence and not retrieved_docs:
            return ""  # 无证据时不注入看板

        lines = ["\n## 已收集证据看板（双源融合）"]

        # === 监控证据（现场实时）===
        if monitoring_evidence:
            lines.append("\n### 监控证据（现场实时）")
            for ev in monitoring_evidence:
                type_label = "指标" if ev.get("type") == "metrics" else "日志"
                service = ev.get("service", "unknown")
                summary = ev.get("summary", "")
                lines.append(f"- [{type_label}] {service}: {summary}")
        else:
            lines.append("\n### 监控证据（现场实时）\n- （尚未查询监控指标/日志）")

        # === 知识库证据（历史经验）===
        if retrieved_docs:
            lines.append("\n### 知识库证据（历史经验）")
            # 按 doc_type 分组展示
            doc_type_labels = {
                "manual": "服务手册",
                "incident": "历史事故",
                "sop": "处置预案",
                "postmortem": "事故复盘",
            }
            by_type: Dict[str, List[Dict]] = {}
            for doc in retrieved_docs:
                dt = doc.get("doc_type", "") or "other"
                by_type.setdefault(dt, []).append(doc)

            for dt, docs in by_type.items():
                label = doc_type_labels.get(dt, dt)
                for doc in docs:
                    idx = doc.get("index", "?")
                    title = doc.get("title", "未知")[:50]
                    snippet = doc.get("content", "")[:60].replace("\n", " ")
                    lines.append(f"- [{label}] {title} [{idx}]: {snippet}...")
        else:
            lines.append("\n### 知识库证据（历史经验）\n- （尚未检索知识库）")

        # === 证据完整性检查 ===
        lines.append("\n### 证据完整性检查")
        has_metrics = any(ev.get("type") == "metrics" for ev in monitoring_evidence)
        has_logs = any(ev.get("type") == "logs" for ev in monitoring_evidence)
        doc_types_collected = set()
        for doc in retrieved_docs:
            dt = doc.get("doc_type", "")
            if dt:
                doc_types_collected.add(dt)

        checks = [
            ("监控指标(metrics)", has_metrics),
            ("监控日志(logs)", has_logs),
            ("服务手册(manual)", "manual" in doc_types_collected),
            ("历史事故(incident)", "incident" in doc_types_collected),
        ]
        for name, done in checks:
            mark = "✓ 已查" if done else "✗ 未查"
            lines.append(f"- {name}: {mark}")

        return "\n".join(lines)

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
        evidence_summary = self._build_evidence_summary(monitoring_evidence, retrieved_docs)
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
        """QA 链路系统提示（P1-1）

        简洁的知识助手模板，不套用诊断工作流。
        - 标准 ReAct：调一次 search_knowledge 后直接回答
        - 无诊断报告格式要求，用普通 Markdown
        - 无工具调用即直接结束（_should_continue 中无 tool_calls → end）
        """
        return """你是一个企业运维知识助手，基于企业知识库回答用户关于运维、架构、开发规范等问题。

## 核心职责
- 基于知识库文档回答通用知识/流程/经验类问题
- 检索到相关文档后简洁作答，用普通 Markdown（列表/段落）
- 不套用故障诊断流程，不追问服务名

## 工具使用
- search_knowledge: 检索知识库文档（通常 1 次即可）
- 非知识类问题（如问候）可不调用工具

## 引用规范
- search_knowledge 结果带 [1]、[2] 编号，回答中可引用
- 不要在末尾重复列出引用列表（系统自动生成）

## 回答风格
- 中文回答，专业、简洁
- Markdown 格式，重要信息加粗
- 代码示例用代码块包裹
- 检索无果时如实说明，不要编造内容
- **禁止过程性文字**：调用工具时只输出 tool_call，不要输出"让我先..."等过程性话语
"""

    def _build_diagnosis_prompt(self) -> str:
        """诊断链路系统提示（保留原完整诊断工作流）"""
        return """你是一个企业运维故障诊断 Agent，专门负责对线上服务故障进行**证据驱动的根因诊断**，并给出可执行的处置方案。

## 核心职责
基于企业运维知识库（服务手册 manual、历史事故 incident、处置预案 sop、事故复盘 postmortem），对用户报告的故障进行系统化诊断：
- 不是简单回答"这是什么"，而是要回答"**为什么发生 + 怎么解决**"
- 所有结论必须**有证据支撑**（来自检索结果），不能凭空推断

## 诊断工作流（必须遵循）
按以下阶段推进诊断，每个阶段都要有明确的工具调用：

### 阶段 1：现象理解
从用户描述中提取关键信息：
- 服务名（service）：如 payment-service、order-service
- 错误现象：如大量 500、延迟飙升、连接拒绝
- 时间范围：如"今天上午""最近1小时"
- 影响范围：如"全量用户""部分接口"
信息不完整时先明确假设的服务名再继续。

### 阶段 2：证据收集（先查实时监控，再查知识库历史经验）

#### 阶段 2A：实时监控取证（优先，调用 query_metrics / query_logs）
线上故障诊断**必须先看实时监控**，拿到现场证据再对照知识库。这是区别于"凭经验猜"的关键。
1. 先查 **关键指标**确认故障范围与方向（调用 query_metrics，metric=all 一次拿全）：
   - `query_metrics(service="<svc>", metric="all", time_range="<按故障时间窗选择>")`
   - **时间窗要与故障对齐**：用户/告警描述"30 分钟前开始报错"→ time_range="30m"；不确定时先用默认 1h，再用 6h/24h 对照（长窗口均值正常 + 短窗口异常 = 近期突发故障）
   - 重点关注：error_rate（故障范围）、connection_pool_usage + pending_connections（连接池是否打满）、qps（是否有流量突增）
2. 根据指标方向**定向查日志**找具体异常（调用 query_logs）：
   - 若 connection_pool_usage 高 → `query_logs(service="<svc>", keyword="HikariPool")` 看连接获取失败/池打满
   - 若疑似慢 SQL → `query_logs(service="mysql", keyword="slow_query")` 看慢查询文本与耗时
   - 若需确认报错面 → `query_logs(service="<svc>", keyword="error")` 看异常堆栈
   - 指标和日志查询之间有依赖关系（日志关键词由指标结果决定），不要盲目并行

#### 阶段 2B：知识库检索（对照历史经验，调用 search_knowledge，必须按 service + doc_type 精准过滤）
拿到监控证据后，再到知识库找历史同类事故与处置方案：
1. 查 **manual**（服务手册）：理解服务架构、依赖、关键指标，印证监控数据是否异常
   - `search_knowledge(query="<服务架构/依赖/关键指标>", service="<svc>", doc_type="manual")`
2. 查 **incident**（历史事故）：找同类历史事故，看当时根因与处置
   - `search_knowledge(query="<错误现象关键词>", service="<svc>", doc_type="incident")`
3. 必要时查 **sop**（处置预案）和 **postmortem**（事故复盘）：找标准处置流程和复盘结论
同一批独立的检索（如 manual + incident）应**并行调用**以提高效率。
4. **历史结论冲突处理**：多条历史文档/事故对同一现象给出不同结论时，优先采用复盘日期
   （effective_date）更新的结论，并在报告"证据"部分注明存在冲突——不要静默选边。

#### 阶段 2C：变更检查（调用 get_recent_changes，变更先于深挖）
**变更是生产故障的第一大根因**。指标取证后必须检查故障时间窗内是否有变更事件：
- `get_recent_changes(service="<svc>", hours="<与故障时间窗匹配，默认 24>")`
- 若故障开始前存在**发版/配置变更/扩缩容**，优先沿"变更 → 影响"因果链定位根因（如"事发前 40 分钟发版新增 DB 查询" + "连接池打满" → 新查询放大连接需求）
- **因果论证义务（防归因偏差）**：把变更定为根因/触发因素必须同时满足两个条件——变更时间**早于**故障起点、变更内容**能解释**指标异常的具体模式；仅有时间先后关系不构成因果
- **显式区分无关变更**：时间上重叠但无法建立解释链的变更，在报告中标注为"同时发生的无关变更"，不要默认归因于最近的发布
- 变更证据与监控证据矛盾时（有变更但指标模式不符合），以监控证据为准并在报告中说明
- 查询变更与知识库检索相互独立，可并行调用

### 阶段 3：根因定位（监控证据 + 变更事件 + 依赖拓扑 + 知识库经验交叉印证）
综合"实时监控 + 变更事件"与"历史经验"推理根因：
- **监控印证**：当前指标（如连接池 100% + pending 87）是否指向某个具体瓶颈？
- **变更对照**：故障时间点附近是否有变更？变更内容能否解释指标异常的**时间起点**？
- **历史对照**：知识库历史事故中是否出现过相同现象？当时的根因是什么？
- **架构解释**：服务手册中描述的依赖、瓶颈点是否能解释当前监控数据？
- **跨服务排查（调用 get_service_dependencies）**：本服务指标无法解释现象、或怀疑问题出在依赖时，
  `get_service_dependencies(service="<svc>")` 获取依赖拓扑，锁定可疑依赖服务后**对其补充取证**
  （query_metrics/query_logs 换成该服务名）；根因跨服务时说明完整因果链（"A 依赖 B，B 的 X 异常导致 A 的 Y"）
- **变更归因纪律**：将根因归于变更时必须给出解释链（变更 → 机制 → 指标异常模式）；
  无法建立解释链的时间重叠变更标注为"同时发生的无关变更"
- 给出**最可能的根因**（而非罗列所有可能），并说明因果链：变更/触发因素 → 瓶颈点 → 根因

### 阶段 4：方案生成
参考历史事故的处置方案 + SOP：
- 短期止血：如何快速恢复服务
- 长期修复：如何避免复发

### 阶段 5：结构化报告输出
最终回答必须按"输出格式"中的结构输出。

## 工具使用规则

### 监控与变更工具（query_metrics / query_logs / analyze_chart / get_recent_changes / get_service_dependencies，故障诊断首选）
- 线上故障诊断**必须先调用 query_metrics 看指标**，拿到现场证据再查知识库
- query_metrics(service, metric="all", time_range) 一次拿全指标，时间窗与故障对齐，避免多次调用
- query_logs 根据指标结果定向查（HikariPool/slow_query/error），关键词由指标方向决定
- get_recent_changes(service, hours) 查故障时间窗内的变更事件，**诊断必查**——变更是第一大根因
- get_service_dependencies(service) 查服务依赖拓扑——本服务指标解释不了现象时查依赖、对依赖补充取证
- 需要理解图表形态（曲线突刺/触顶/跨指标关联）时 → analyze_chart(service)，VLM 看图输出异常模式与洞察
- 监控数据是"现场证据"，变更事件是"触发因素"，依赖拓扑是"影响面地图"，知识库是"历史经验"，交叉印证才能定位根因

### 必须调用 search_knowledge 的场景
任何运维诊断问题，**在查完监控后必须检索知识库**再回答。诊断流程中至少调用 2 次 search_knowledge（manual + incident）。

### 精准过滤（重要）
诊断时**优先用 service + doc_type 过滤**，避免全库噪声：
- service: 限定服务名（如 "payment-service"）
- doc_type: "manual" | "incident" | "sop" | "postmortem"
非诊断类问题（如问候、能力询问）可不调用工具。

### 其他工具
- 最新版本/外部信息 → web_search（互联网更及时）
- **创建工单** → create_incident_ticket：仅当 P1/P2 级故障诊断完成、或用户明确要求时调用（诊断结论落地跟进），诊断中途不要调用
- 用户画像/记忆 → get_user_profile / search_memory（诊断场景少用）
- 保存诊断结论 → save_memory

## 引用规范（重要）
- search_knowledge 结果带 [1]、[2] 编号
- 诊断报告"证据"部分**必须标注引用**：如"该服务依赖 MySQL 连接池 [1]"
- 引用编号必须与 search_knowledge 返回的序号对应
- 不要在报告末尾重复列出引用列表（系统自动生成引用卡片）

## 输出格式（结构化诊断报告）
最终回答必须严格按以下 Markdown 结构输出，不要增减一级标题：

### 现象
<复述用户报告的现象 + 提取的关键信息：服务/错误/时间/影响>

### 证据
<列出收集到的证据，包括监控数据和历史经验，每条带引用 [N]>
- 监控证据1：query_metrics 返回 error_rate=38% / connection_pool_usage=100%
- 监控证据2：query_logs 返回 HikariPool "Connection is not available"
- 知识库证据1：该服务依赖 MySQL 连接池 [1]
- 知识库证据2：INC-2026-001 历史事故同为连接池耗尽 [2]

### 根因分析
<基于证据推理的最可能根因，明确因果链>

### 处置方案
**短期止血**：
1. ...

**长期修复**：
1. ...

### 置信度
<高/中/低> - <理由：如"有 2 条历史事故支持同一根因 [1][2]">

## 重要约束
- **监控优先**：故障诊断必须先 query_metrics 看实时指标，再查知识库，不能跳过监控直接凭经验检索
- **证据驱动**：根因和方案必须有监控数据 + 检索证据支撑，不能凭空推断
- **精准过滤**：诊断时必须用 service+doc_type，避免全库噪声
- **高效检索**：每次查询必须是不同 doc_type 或不同角度，最多搜 3 次
- **能并行就并行**：独立的检索并行执行（监控指标与日志有依赖则不并行）
- **没找到也要说**：某类文档未找到时明确说明"知识库中无该服务的历史事故"，基于已有证据+通用经验给出低置信度判断
- **禁止过程性文字（最重要）**：在调用工具、收集证据的过程中**不要输出任何文字**，只输出 tool_call。不允许出现"让我先...""我理解你的意思""请告诉我服务名"之类的过程性话语。你的所有文字只能出现在**最终一次回答**里——即要么是结构化诊断报告，要么是对非诊断问题的直接答复。中途的检索/思考不要写成文字。

## 回答风格
- 中文回答，专业、简洁
- Markdown 格式，重要信息加粗
- 代码示例用代码块包裹
"""

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

    @staticmethod
    def _compute_evidence_sufficiency(
        monitoring_evidence: List[Dict],
        citations: List[Dict],
        tools_used: List[str],
        diagnosis_report: Optional[Dict],
        mcp_degraded: bool = False,
    ) -> Dict[str, Any]:
        """规则计算的"证据充分度"（校准 LLM 自报置信度的过度自信）

        与 LLM 置信度的关系：置信度是模型对"我的结论对不对"的主观自评，
        充分度是"这次诊断拿到的客观证据够不够"的规则化度量——两者正交，
        展示时取较低者作为建议采信级别。

        因子（满分 100）：
        - 监控取证 30（metrics/logs/alerts/chart 各 15，封顶 30）
        - 知识库命中 25（≥2 条引用 25，1 条 12）
        - 变更检查 15（get_recent_changes 已执行且有事件或明确查过）
        - 跨服务拓扑 10（get_service_dependencies 已执行）
        - 报告完整性 20（根因+方案 20，仅有其一 10）
        - 监控源降级 → 总分封顶 50（对应 prompt 层"置信度最高中"的数值化）
        """
        factors: Dict[str, Any] = {}
        score = 0

        # 1. 监控取证（按证据类型计数，封顶 30）
        mon_types = {e.get("type") for e in (monitoring_evidence or []) if e.get("type")}
        mon_score = min(30, 15 * len(mon_types))
        score += mon_score
        factors["monitoring"] = {"score": mon_score, "types": sorted(mon_types)}

        # 2. 知识库命中
        cite_count = len(citations or [])
        kb_score = 25 if cite_count >= 2 else (12 if cite_count == 1 else 0)
        score += kb_score
        factors["knowledge"] = {"score": kb_score, "citations": cite_count}

        # 3. 变更检查
        tools = set(tools_used or [])
        change_score = 15 if "get_recent_changes" in tools else 0
        score += change_score
        factors["change_check"] = {"score": change_score}

        # 4. 跨服务拓扑
        topo_score = 10 if "get_service_dependencies" in tools else 0
        score += topo_score
        factors["topology"] = {"score": topo_score}

        # 5. 报告完整性
        if diagnosis_report:
            has_root = bool(diagnosis_report.get("root_cause"))
            has_solution = bool(diagnosis_report.get("solution"))
            report_score = 20 if (has_root and has_solution) else (10 if (has_root or has_solution) else 0)
        else:
            report_score = 0
        score += report_score
        factors["report_complete"] = {"score": report_score}

        # 6. 监控源降级封顶
        capped = False
        if mcp_degraded:
            if score > 50:
                score = 50
                capped = True
        factors["mcp_degraded"] = {"capped": capped, "degraded": bool(mcp_degraded)}

        level = "high" if score >= 70 else ("medium" if score >= 40 else "low")
        return {"score": score, "level": level, "factors": factors}

    def _get_sufficiency_for_run(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """从 run 的结果组件计算证据充分度（含 MCP 降级判定）"""
        mcp_status = getattr(self, "_mcp_status", "ok")
        degraded = mcp_status in ("failed", "timeout")
        return self._compute_evidence_sufficiency(
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
            diagnosis_report = self._parse_diagnosis_report(final_content) if intent == "diagnosis" else None

            result = {
                "content": final_content,
                "tools_used": final_state.get("tools_used", []),
                "citations": self._build_citations(final_state.get("retrieved_docs", [])),
                "monitoring_evidence": final_state.get("monitoring_evidence", []),
                "diagnosis_report": diagnosis_report,
                "step_count": final_state.get("step_count", 0),
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
            logger.error(f"Agent 执行失败: {e}", exc_info=True)
            return {
                "content": "抱歉，处理过程中出现内部错误，请稍后重试",
                "tools_used": [],
                "citations": [],
                "monitoring_evidence": [],
                "diagnosis_report": None,
                "step_count": 0,
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
                        diagnosis_report = self._parse_diagnosis_report(final_content)

                yield {
                    "type": "done",
                    "tools_used": tools_used,
                    "step_count": step_count,
                    "citations": self._build_citations(values.get("retrieved_docs", [])),
                    "monitoring_evidence": values.get("monitoring_evidence", []),
                    "diagnosis_report": diagnosis_report,
                }

        except Exception as e:
            logger.error(f"流式执行失败: {e}")
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
