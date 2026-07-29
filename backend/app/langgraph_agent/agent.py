"""
LangGraph Agent

基于 LangGraph 实现的 Agent 系统。
"""

from typing import List, Dict, Any, Optional, Callable
from loguru import logger

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, trim_messages
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    HAS_ASYNC_SQLITE = True
except ImportError:
    HAS_ASYNC_SQLITE = False
    logger.warning("aiosqlite 未安装，无法使用 SQLite 持久化")

from .state import AgentState
from .tools import create_tools, set_retriever, set_knowledge_store


class LangGraphAgent:
    """
    LangGraph Agent

    基于 LangGraph 实现的 Agent 系统，支持：
    - 自主决策
    - 工具调用
    - RAG 检索
    - 反思机制
    """

    def __init__(
        self,
        llm_model: str = "deepseek-chat",
        llm_base_url: str = "https://api.deepseek.com",
        llm_api_key: str = "dummy",
        max_steps: int = 8,
        knowledge_store=None,
        checkpoint_path: str = None,
    ):
        self.max_steps = max_steps

        # 初始化 LLM（支持并行工具调用）
        self.llm = ChatOpenAI(
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0.7,
            model_kwargs={"parallel_tool_calls": True},  # 启用并行工具调用
        )

        # 初始化工具
        self.tools = create_tools()
        self.tool_node = ToolNode(self.tools)

        # 绑定工具到 LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # 设置知识库
        if knowledge_store:
            set_knowledge_store(knowledge_store)

        # 初始化 Checkpoint（会话记忆）
        self.checkpoint_path = checkpoint_path
        if checkpoint_path:
            import os
            os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
            self.memory = self._create_async_sqlite_saver(checkpoint_path)
        else:
            self.memory = MemorySaver()
            logger.info("Checkpoint 使用内存存储（重启后丢失）")

        # 构建图
        self.graph = self._build_graph()

        logger.info(f"LangGraph Agent 初始化完成，模型: {llm_model}")

    def _create_async_sqlite_saver(self, db_path: str):
        """创建异步 SQLite Saver"""
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            # 创建连接并初始化表
            conn = aiosqlite.connect(db_path)
            saver = AsyncSqliteSaver(conn)
            logger.info(f"Checkpoint 持久化到: {db_path}")
            return saver
        except Exception as e:
            logger.warning(f"SQLite 持久化失败，回退到内存: {e}")
            return MemorySaver()

    def _build_graph(self) -> StateGraph:
        """构建 LangGraph 图"""
        # 创建图
        workflow = StateGraph(AgentState)

        # 添加节点
        workflow.add_node("agent", self._call_agent)
        workflow.add_node("tools", self.tool_node)
        workflow.add_node("reflection", self._reflect)

        # 设置入口
        workflow.set_entry_point("agent")

        # 添加条件边
        workflow.add_conditional_edges(
            "agent",
            self._should_continue,
            {
                "continue": "tools",
                "reflect": "reflection",
                "end": END,
            },
        )

        # 工具执行后回到 agent
        workflow.add_edge("tools", "agent")

        # 反思后决定是否继续
        workflow.add_conditional_edges(
            "reflection",
            self._after_reflection,
            {
                "continue": "agent",
                "end": END,
            },
        )

        # 编译图（添加 checkpoint 支持会话记忆）
        return workflow.compile(checkpointer=self.memory)

    def _call_agent(self, state: AgentState) -> Dict:
        """调用 Agent（LLM）"""
        messages = state["messages"]
        step_count = state.get("step_count", 0)
        tools_used = state.get("tools_used", [])
        task_context = state.get("task_context", {})
        user_id = task_context.get("user_id")

        # 添加系统提示（注入记忆上下文）
        system_prompt = self._build_system_prompt(state, user_id=user_id)

        # 构建消息
        full_messages = [SystemMessage(content=system_prompt)] + messages

        # 调用 LLM
        try:
            response = self.llm_with_tools.invoke(full_messages)
            logger.info(f"Agent Step {step_count + 1}: LLM 响应")
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            response = AIMessage(content=f"抱歉，处理过程中出现错误: {str(e)}")

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
        }

    def _should_continue(self, state: AgentState) -> str:
        """决定是否继续执行"""
        messages = state["messages"]
        last_message = messages[-1]
        step_count = state.get("step_count", 0)

        # 达到最大步数
        if step_count >= self.max_steps:
            logger.info(f"达到最大步数 {self.max_steps}，结束")
            return "end"

        # 有工具调用
        if last_message.tool_calls:
            # 记录并行工具调用
            if len(last_message.tool_calls) > 1:
                tool_names = [tc.get('name', 'unknown') for tc in last_message.tool_calls]
                logger.info(f"并行工具调用: {tool_names}")

            # 每 3 步反思一次（优化决策）
            if step_count > 0 and step_count % 3 == 0:
                return "reflect"
            return "continue"

        # 没有工具调用 → 结束
        return "end"

    def _reflect(self, state: AgentState) -> Dict:
        """反思"""
        messages = state["messages"]
        step_count = state.get("step_count", 0)

        # 清理消息：移除未完成的 tool_calls 序列
        cleaned_messages = self._clean_messages_for_reflection(messages)

        # 构建反思提示
        reflection_prompt = f"""
你已经执行了 {step_count} 步。请评估：

1. 用户问的是通用知识/编程概念，还是涉及私有知识库/最新信息？
2. 如果是通用问题，直接基于你的训练知识回答，不要再调用工具。
3. 如果已经检索过但没有找到理想结果，基于已有信息和你的通用知识直接回答，不要再说"信息不足"。
4. 如果确实需要更多信息，可以选择调用不同角度的工具，但不要重复已调用过的工具。

请直接给出对用户的最终回答或下一步行动计划。
"""

        # 添加反思提示
        cleaned_messages.append(HumanMessage(content=reflection_prompt))

        # 调用 LLM
        try:
            response = self.llm.invoke(cleaned_messages)
        except Exception as e:
            logger.error(f"反思失败: {e}")
            response = AIMessage(content="继续执行...")

        return {
            "messages": [response],
            "reflection": {
                "step": step_count,
                "suggestion": response.content[:200],
            },
        }

    def _clean_messages_for_reflection(self, messages):
        """清理消息，移除未完成的 tool_calls 序列"""
        cleaned = []
        i = 0
        while i < len(messages):
            msg = messages[i]

            # 如果是 AIMessage 且有 tool_calls
            if isinstance(msg, AIMessage) and msg.tool_calls:
                # 检查后面是否有对应的 ToolMessage
                has_tool_response = False
                for j in range(i + 1, min(i + 10, len(messages))):
                    if isinstance(messages[j], ToolMessage):
                        # 检查 tool_call_id 是否匹配
                        if any(tc['id'] == messages[j].tool_call_id for tc in msg.tool_calls):
                            has_tool_response = True
                            break
                    # 如果遇到下一个 AIMessage，停止查找
                    if isinstance(messages[j], AIMessage):
                        break

                # 如果有完整的 tool_calls 序列，保留
                if has_tool_response:
                    cleaned.append(msg)
                # 否则跳过这个 AIMessage（不完整）
            else:
                cleaned.append(msg)

            i += 1

        return cleaned

    def _after_reflection(self, state: AgentState) -> str:
        """反思后决定是否继续"""
        messages = state["messages"]
        last_message = messages[-1]

        # 如果 LLM 决定直接回答
        if not last_message.tool_calls:
            return "end"

        return "continue"

    def _build_system_prompt(self, state: AgentState, user_id: str = None) -> str:
        """构建系统提示（注入记忆上下文）"""
        tools_used = state.get("tools_used", [])
        retrieved_docs = state.get("retrieved_docs", [])
        task_context = state.get("task_context", {})
        use_web_search = task_context.get("use_web_search", False)

        prompt = """你是一个智能个人助手，擅长信息收集、内容生成、任务拆解和知识管理。

## 你的能力
1. **信息收集**: 搜索知识库、搜索互联网、爬取网页
2. **内容生成**: 撰写文章、生成文档、总结内容
3. **知识管理**: 管理和检索知识库

## 工具使用规则

### 智能选择（根据问题类型选择合适的工具）
- **简单问候/闲聊/通用知识问题** → 不调用工具，直接用你的知识回答
- **项目知识库/个人文档相关问题** → 用 search_knowledge（查询知识库）
- **最新信息/新闻/实时数据** → 用 web_search（互联网更及时）
- **需要详细网页内容** → 用 crawl_webpage（爬取完整页面）
- **生成文章/内容** → 用 generate_content（生成文章）
- **用户画像/记忆** → 用 get_user_profile / search_memory
- **保存重要信息** → 用 save_memory

### 并行调用（提高效率）
当确实需要多个来源的信息时，**同时调用多个工具**：
- 问"我上传的文档里是怎么讲自动校验的？再顺便看看网上最新说法" → 同时调用 [search_knowledge, web_search]
- 问"帮我总结这个网页并保存" → 同时调用 [crawl_webpage, save_memory]
- 问"我的学习情况如何？" → 同时调用 [get_user_profile, search_memory]

注意：通用知识问题（如"FastAPI 是什么""自动校验的原理"）**不需要**调用 search_knowledge，直接用你的知识回答即可。

## 回答风格
- 用中文回答，语气专业、友好
- 使用 Markdown 格式美化输出
- 重要信息用 **加粗** 标注
- 引用来源时标注出处

## 回答原则
- 对于**通用概念、编程技术、框架原理、软件工程常识**等常见问题，**直接用你的训练知识回答**，不要强行检索知识库。
- 只有当问题明显涉及**用户的私有知识库、个人记忆、最新事件或需要外部工具才能完成**时，才调用工具。
- 如果调用了 `search_knowledge` 但没有找到相关内容，**不要回答"信息不足"**，而是基于你的通用知识给出完整回答，并可以补充说明"知识库中未找到相关文档"。
- 不要连续多次重复调用同一个工具，最多尝试 2 次。

## 重要约束
- **搜索要高效**: 每次查询必须是不同的角度
- **搜完就用**: 搜索到的信息即使不完美，也要用来回答
- **最多搜2次**: 如果搜了2次还找不到满意的信息，就用已有信息回答
- **能并行就并行**: 独立的工具调用尽量并行执行，提高效率
"""

        # 注入用户记忆上下文
        if user_id:
            try:
                from ..shared_services import get_memory_manager
                memory = get_memory_manager()
                if memory:
                    # 用户画像
                    profile = memory.core_memory.get_user_profile(user_id)
                    profile_context = profile.to_context_string()
                    if profile_context:
                        prompt += f"\n## 用户信息\n{profile_context}\n"

                    # 最近对话历史
                    history = memory.recall_memory.get_recent_history(user_id, limit=5)
                    if history:
                        prompt += "\n## 最近对话\n"
                        for turn in history[-5:]:
                            role_name = "用户" if turn.role == "user" else "AI"
                            prompt += f"{role_name}: {turn.content[:100]}\n"

                    # 相关档案记忆
                    last_user_msg = ""
                    for msg in reversed(state.get("messages", [])):
                        if hasattr(msg, 'content') and hasattr(msg, 'type') and msg.type == 'human':
                            last_user_msg = msg.content
                            break
                    if last_user_msg:
                        archival = memory.archival_memory.search(last_user_msg, user_id, top_k=3)
                        if archival:
                            prompt += "\n## 相关记忆\n"
                            for i, entry in enumerate(archival, 1):
                                cat = f"[{entry.category}] " if entry.category else ""
                                prompt += f"{i}. {cat}{entry.content[:150]}\n"
            except Exception as e:
                logger.debug(f"注入记忆上下文失败: {e}")

        # 根据 use_web_search 标志调整策略
        if use_web_search:
            prompt += "\n## 搜索策略\n用户启用了联网搜索，优先使用 web_search 获取最新信息。\n"

        # 添加已使用工具信息
        if tools_used:
            prompt += f"\n## 已使用工具\n{', '.join(tools_used)}\n"

        return prompt

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
            "retrieved_docs": [],
            "citations": [],
            "reflection": None,
            "should_retry": False,
            "step_count": 0,
            "max_steps": self.max_steps,
        }

        try:
            # 执行图（传入 config 以使用 checkpoint）
            final_state = await self.graph.ainvoke(initial_state, config=config)

            # 提取结果
            messages = final_state["messages"]
            last_message = messages[-1]

            return {
                "content": last_message.content,
                "tools_used": final_state.get("tools_used", []),
                "citations": final_state.get("citations", []),
                "step_count": final_state.get("step_count", 0),
                "reflection": final_state.get("reflection"),
            }

        except Exception as e:
            logger.error(f"Agent 执行失败: {e}")
            return {
                "content": f"抱歉，处理过程中出现错误: {str(e)}",
                "tools_used": [],
                "citations": [],
                "step_count": 0,
                "reflection": None,
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
        - {"type": "reflection"}                  反思阶段
        - {"type": "done", "tools_used": [...], "step_count": N} 完成

        Args:
            user_input: 用户输入
            session_id: 会话 ID
            context: 额外上下文
            use_web_search: 是否使用 Web Search

        Yields:
            Dict: 流式事件
        """
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
            "retrieved_docs": [],
            "citations": [],
            "reflection": None,
            "should_retry": False,
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
            current_llm_content = ""    # 当前 LLM 调用的累积内容（用于判断是否为最终回答）

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

                    # 检查是否为 tool_calls（tool_calls 通常没有 content，而是有 tool_call_chunks）
                    has_tool_calls = (
                        hasattr(chunk, "tool_call_chunks")
                        and chunk.tool_call_chunks
                    )

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

                # 4. 节点结束事件（用于标记 reflection 阶段）
                elif kind == "on_chain_end" and name == "reflection":
                    yield {"type": "reflection", "content": "正在反思..."}

            # 获取最终状态
            final_state_values = await self.graph.aget_state(config)
            if final_state_values and final_state_values.values:
                values = final_state_values.values
                messages = values.get("messages", [])
                tools_used = values.get("tools_used", [])
                step_count = values.get("step_count", 0)

                # 如果 token 流没有覆盖完整内容（例如 LLM 直接返回没走 stream）
                # 用最终 state 的最后一条消息补齐
                if messages and not current_llm_content:
                    last_msg = messages[-1]
                    if hasattr(last_msg, "content") and last_msg.content:
                        yield {"type": "token", "content": last_msg.content}

                yield {
                    "type": "done",
                    "tools_used": tools_used,
                    "step_count": step_count,
                }

        except Exception as e:
            logger.error(f"流式执行失败: {e}")
            yield {
                "type": "error",
                "content": f"抱歉，处理过程中出现错误: {str(e)}",
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
