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
    logger.warning("langgraph-checkpoint-sqlite 未安装，无法使用 SQLite 持久化")

from .state import AgentState
from .tools import (
    create_tools,
    set_retriever,
    set_knowledge_store,
    pop_retrieval_buffer,
    set_conversation_context,
    set_current_user_id,
    set_query_rewriter_llm,
)


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
        max_context_tokens: int = 32000,
        reserved_for_output: int = 4000,
    ):
        self.max_steps = max_steps
        self.llm_model = llm_model

        # 初始化 LLM（支持并行工具调用）
        self.llm = ChatOpenAI(
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0.3,  # 降低温度：生成更稳定且更短，减少耗时
            max_tokens=1500,  # 限制输出长度：避免冗长生成，缩短响应时间
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

        # 注入查询重写 LLM（复用主 LLM，用于多轮对话指代消解）
        # 用低温度配置，确保重写结果稳定
        from langchain_openai import ChatOpenAI as _ChatOpenAI
        query_rewriter = _ChatOpenAI(
            model=llm_model,
            base_url=llm_base_url,
            api_key=llm_api_key,
            temperature=0.0,  # 确保重写结果稳定一致
            max_tokens=100,   # 重写查询不需要长输出
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

        logger.info(
            f"LangGraph Agent 初始化完成，模型: {llm_model}，"
            f"上下文预算: {max_context_tokens} tokens（预留输出 {reserved_for_output}）"
        )

    async def _ensure_saver(self):
        """延迟初始化 AsyncSqliteSaver（需在异步上下文中调用）

        langgraph 0.4.x 中 AsyncSqliteSaver 需要 aiosqlite.Connection，
        只能在异步上下文中创建。首次 run() 时从 MemorySaver 切换为 SQLite 持久化。
        """
        if self._saver_initialized or not self.checkpoint_path:
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
        """构建 LangGraph 图"""
        # 创建图
        workflow = StateGraph(AgentState)

        # 添加节点
        workflow.add_node("agent", self._call_agent)
        workflow.add_node("tools", self.tool_node)
        workflow.add_node("reflect_node", self._reflect)

        # 设置入口
        workflow.set_entry_point("agent")

        # 添加条件边
        workflow.add_conditional_edges(
            "agent",
            self._should_continue,
            {
                "continue": "tools",
                "reflect": "reflect_node",
                "end": END,
            },
        )

        # 工具执行后回到 agent
        workflow.add_edge("tools", "agent")

        # 反思后决定是否继续
        workflow.add_conditional_edges(
            "reflect_node",
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

        # 引用溯源：收集检索结果（两个来源合并去重）
        # 1. 模块级 buffer（主要方案：search_knowledge 工具写入）
        buffered_docs = pop_retrieval_buffer()
        # 2. ToolMessage artifact（备用方案：未来 LangGraph 版本可能支持）
        artifact_docs = self._collect_retrieved_docs(messages)
        # 合并已有 + 新增，按 doc_id + content 去重
        existing = state.get("retrieved_docs", [])
        retrieved_docs = self._merge_retrieved_docs(existing + buffered_docs + artifact_docs)

        # 多轮对话：注入对话上下文（供 search_knowledge 做指代消解）
        # 必须在 LLM 调用前设置，确保工具执行时能读到对话历史
        set_conversation_context(messages)
        # 注入当前用户身份（供 search_knowledge 做文档权限过滤）
        # 确保工具执行时只能检索到该用户有权访问的文档
        set_current_user_id(user_id)

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
            "retrieved_docs": retrieved_docs,
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
        seen: set = set()
        unique: List[Dict] = []
        for doc in retrieved_docs:
            dedup_key = (doc.get("doc_id", ""), doc.get("content", "")[:100])
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            unique.append(doc)

        # 按分数降序排序
        unique.sort(key=lambda x: x.get("score", 0), reverse=True)

        # 重新编号并精简字段（去掉 content 避免响应过大）
        citations = []
        for i, doc in enumerate(unique, 1):
            citations.append({
                "index": i,
                "doc_id": doc.get("doc_id", ""),
                "title": doc.get("title", ""),
                "heading_path": doc.get("heading_path", ""),
                "score": doc.get("score", 0),
                "source": doc.get("source", "knowledge_base"),
                "image_path": doc.get("image_path"),
            })
        return citations

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

    def _trim_messages_to_budget(self, messages: List) -> List:
        """按 token 预算从后往前裁剪消息，保留最近的消息。

        核心策略：
        1. SystemMessage 始终保留（不在裁剪范围内，由调用方单独处理）
        2. 从最后一条消息往前累加 token，达到预算时停止
        3. 修复裁剪边界可能破坏的 tool_calls 配对：
           - 移除开头孤立的 ToolMessage（对应的 AIMessage 已被裁掉）
           - 移除开头孤立的 AIMessage(tool_calls)（对应的 ToolMessage 已被裁掉）

        Args:
            messages: 待裁剪的消息列表（不含 SystemMessage，或 SystemMessage 已在首位）

        Returns:
            裁剪后的消息列表，token 总数不超过 max_context_tokens - reserved_for_output
        """
        if not messages:
            return messages

        budget = self.max_context_tokens - self.reserved_for_output
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

        # 上下文管理：反思也需要裁剪，避免长对话超限
        # 反思用较小预算（只需最近上下文判断是否需要继续）
        original_budget = self.max_context_tokens
        self.max_context_tokens = min(original_budget, 16000)  # 反思限 16K
        try:
            trimmed = self._trim_messages_to_budget(cleaned_messages)
        finally:
            self.max_context_tokens = original_budget  # 恢复原预算

        # 调用 LLM
        try:
            response = self.llm.invoke(trimmed)
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

        prompt = """你是一个企业知识库问答助手，基于企业内部知识库文档回答问题。

## 核心职责
你的首要任务是**检索企业知识库**并基于检索结果回答用户问题。知识库中包含企业内部文档（API文档、运维SOP、架构设计、开发指南、故障排查等），这些是回答问题的权威来源。

## 工具使用规则

### 知识库优先（默认行为）
对于**任何知识性问题**（技术、运维、流程、架构、规范、故障排查等），**必须先调用 search_knowledge 检索知识库**，再基于检索结果回答。

- **必须调用 search_knowledge 的场景**：
  - 技术问题（如"FastAPI怎么定义路由""如何部署服务"）
  - 运维操作（如"数据库备份怎么做""如何重启服务"）
  - 流程规范（如"上线流程是什么""代码规范要求"）
  - 架构设计（如"系统架构是怎样的""模块间如何交互"）
  - 故障排查（如"报错XXX怎么处理""服务起不来怎么办"）
  - 任何涉及企业内部文档、SOP、指南的问题

- **可以不调用工具的场景**（仅限以下情况）：
  - 纯粹的问候/闲聊（如"你好""谢谢"）
  - 关于你自身能力的询问（如"你能做什么"）
  - 用户明确表示不需要知识库的创造性任务（如"帮我写一首诗"）

### 其他工具
- **最新信息/新闻/实时数据** → 用 web_search（互联网更及时）
- **需要详细网页内容** → 用 crawl_webpage（爬取完整页面）
- **生成文章/内容** → 用 generate_content（生成文章）
- **用户画像/记忆** → 用 get_user_profile / search_memory
- **保存重要信息** → 用 save_memory

### 并行调用（提高效率）
当确实需要多个来源的信息时，**同时调用多个工具**：
- 问"知识库里怎么讲的？顺便看看网上最新说法" → 同时调用 [search_knowledge, web_search]
- 问"帮我总结这个网页并保存" → 同时调用 [crawl_webpage, save_memory]

## 引用规范（重要）
- 调用 search_knowledge 后，结果带有 [1]、[2] 等编号
- 回答时**必须在对应位置标注内联引用**，例如：FastAPI 使用装饰器定义路由 [1]
- 引用编号必须与 search_knowledge 返回的序号对应
- 不要在回答末尾重复列出引用列表（系统会自动生成引用来源卡片）
- **基于知识库回答时必须带引用编号**，这是衡量回答质量的关键指标

## 回答原则
- **知识库优先**：知识库文档是回答的权威来源，优先基于检索结果回答
- **补充而非替代**：知识库未覆盖的部分，可以用你的通用知识补充，但要明确区分
- **检索失败处理**：如果 search_knowledge 返回"未找到相关知识"，可以基于通用知识回答，并说明"知识库中暂无相关文档"
- **不要连续多次重复调用同一个工具**，最多尝试 2 次

## 回答风格
- 用中文回答，语气专业、友好
- 使用 Markdown 格式美化输出
- 重要信息用 **加粗** 标注
- 代码示例用代码块包裹

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
                "citations": self._build_citations(final_state.get("retrieved_docs", [])),
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
                elif kind == "on_chain_end" and name == "reflect_node":
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
                    "citations": self._build_citations(values.get("retrieved_docs", [])),
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
