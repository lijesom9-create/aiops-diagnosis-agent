"""
Agent 图流转端到端测试（专题：端到端测试）

验证 LangGraph 图的节点路由与状态流转，聚焦运维诊断的完整链路：
  监控取证 → 知识库检索 → 诊断报告输出

测试策略：
- mock LLM（llm_with_tools）的 invoke 返回预设 AIMessage 序列
- mock ToolNode 返回预设 ToolMessage（不依赖真实工具实现）
- 通过 graph.ainvoke 执行完整图流转，验证最终状态

覆盖场景：
1. 完整诊断链路：监控→知识库→诊断报告（正常路径）
2. 直接诊断路径：无工具调用→直接结束（简单问题）
3. max_steps 终止：达最大步数强制结束
4. MCP 降级提示注入：_mcp_status 影响系统提示
5. 工具调用顺序验证：监控优先于知识库

设计说明：
- 不调用真实 LLM，用 side_effect 列表控制 LLM 响应序列
- 不依赖真实工具/MCP/知识库，ToolNode 返回 mock 数据
- 聚焦图的路由逻辑与状态流转正确性

用法：
    cd backend
    python -m pytest tests/test_agent_graph_flow.py -v
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.langgraph_agent.evidence import _extract_monitoring_evidence

try:
    import certifi
    _correct_ca = certifi.where()
    if not os.path.exists(os.environ.get("REQUESTS_CA_BUNDLE", "")):
        os.environ["REQUESTS_CA_BUNDLE"] = _correct_ca
        os.environ["SSL_CERT_FILE"] = _correct_ca
except Exception:
    pass

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# ========== Mock ToolNode ==========

class MockToolNode:
    """模拟 LangGraph ToolNode，根据 tool_calls 返回预设 ToolMessage

    真实 ToolNode 调用 tool.invoke() 执行工具并返回 ToolMessage。
    这里直接根据 tool_call 的 name 返回 mock 的 JSON 结果，
    避免 search_knowledge 依赖 knowledge_store、query_metrics 依赖 MCP。

    实现 __call__ 使其成为 callable，LangGraph 会包装为 RunnableLambda。
    """

    def __init__(self):
        self.call_log = []  # 记录工具调用顺序

    def __call__(self, state):
        """callable 接口（LangGraph 会自动包装为 RunnableLambda）"""
        messages = state["messages"]
        last_message = messages[-1]
        tool_messages = []

        for tc in (last_message.tool_calls or []):
            tool_name = tc.get("name", "unknown")
            tc_id = tc.get("id", "call_id")
            args = tc.get("args", {})
            self.call_log.append(tool_name)

            # 根据工具名返回 mock 结果
            if tool_name == "query_metrics":
                content = json.dumps({
                    "service": args.get("service", "unknown"),
                    "metrics": {"connection_pool_usage": "100%", "error_rate": "15%"},
                    "status": "critical",
                }, ensure_ascii=False)
            elif tool_name == "query_logs":
                content = json.dumps({
                    "service": args.get("service", "unknown"),
                    "logs": [{"level": "ERROR", "msg": "HikariPool-1 - Connection is not available"}],
                }, ensure_ascii=False)
            elif tool_name == "search_knowledge":
                content = json.dumps({
                    "results": [
                        {"doc_id": "d1", "title": "mysql 连接池配置手册",
                         "content": "HikariCP maximum-pool-size 默认 10..."},
                        {"doc_id": "d2", "title": "连接池耗尽事故复盘",
                         "content": "2024-06 连接池耗尽导致 500 错误..."},
                    ],
                }, ensure_ascii=False)
            else:
                content = json.dumps({"result": f"mock result for {tool_name}"})

            tool_messages.append(ToolMessage(content=content, tool_call_id=tc_id))

        return {"messages": tool_messages}


# ========== Fixture ==========

@pytest.fixture
def mock_agent():
    """构造 mock Agent：替换 LLM、ToolNode、辅助方法，重建图

    返回 (agent, mock_tool_node)，测试可通过 mock_tool_node.call_log 检查工具调用顺序
    """
    from app.langgraph_agent.agent import LangGraphAgent

    agent = LangGraphAgent(
        llm_model="mock-model",
        llm_base_url="http://localhost",
        llm_api_key="dummy",
        max_steps=8,
        knowledge_store=None,
        checkpoint_path=None,  # 用 MemorySaver，避免 SQLite
    )

    # 替换 ToolNode
    mock_tool_node = MockToolNode()
    agent.tool_node = mock_tool_node

    # 替换 LLM 为 MagicMock（Pydantic 模型不允许直接设 invoke 属性）
    # _call_agent 用 self.llm_with_tools.invoke()
    agent.llm_with_tools = MagicMock()

    # 辅助方法 mock：避免依赖 tokenizer 逻辑
    agent._trim_messages_to_budget = MagicMock(side_effect=lambda msgs: msgs)

    # 重建图（使用 mock tool_node；_build_graph 不涉及 llm_with_tools 的重新绑定）
    agent.graph = agent._build_graph()

    return agent, mock_tool_node


def _make_tool_call(name: str, args: dict, call_id: str):
    """构造 tool_call 字典"""
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _make_initial_state(message: str = "为什么 mysql 连接池报错？"):
    """构造图的初始状态"""
    return {
        "messages": [HumanMessage(content=message)],
        "tools_used": [],
        "tool_results": {},
        "task_type": "",
        "task_context": {"user_id": "test_user"},
        "retrieved_docs": [],
        "citations": [],
        "monitoring_evidence": [],
        "diagnosis_report": None,
        "step_count": 0,
        "max_steps": 8,
    }


# ========== 完整诊断链路测试 ==========

class TestFullDiagnosisFlow:
    """完整诊断链路：监控取证 → 知识库检索 → 诊断报告"""

    @pytest.mark.asyncio
    async def test_monitoring_then_knowledge_then_report(self, mock_agent):
        """监控→知识库→诊断报告的完整链路（P2 去反思后）

        LLM 调用序列：
        1. query_metrics tool_call → tools
        2. search_knowledge tool_call → tools
        3. 无 tool_calls 的最终回答（含诊断报告）→ end
        """
        agent, mock_tool_node = mock_agent

        # mock LLM 响应序列
        diagnosis_content = (
            "### 现象\nmysql 连接池耗尽\n"
            "### 证据\n监控: 连接池100% [1] 连接池手册\n"
            "### 根因分析\nHikariPool 最大连接数过低\n"
            "### 处置方案\n调大 maximum-pool-size\n"
            "### 置信度\n高"
        )
        agent.llm_with_tools.invoke = MagicMock(side_effect=[
            # Step 1: 调用 query_metrics
            AIMessage(content="", tool_calls=[
                _make_tool_call("query_metrics", {"service": "mysql", "metric": "all"}, "call_1")
            ]),
            # Step 2: 调用 search_knowledge
            AIMessage(content="", tool_calls=[
                _make_tool_call("search_knowledge", {"query": "mysql 连接池配置"}, "call_2")
            ]),
            # Step 3: 最终回答（无 tool_calls → end）
            AIMessage(content=diagnosis_content),
        ])

        # mock pop_retrieval_buffer 避免依赖模块级 buffer
        with patch("app.langgraph_agent.agent.pop_retrieval_buffer", return_value=[]), \
             patch("app.langgraph_agent.agent.set_conversation_context"), \
             patch("app.langgraph_agent.agent.set_current_user_id"):
            final_state = await agent.graph.ainvoke(
                _make_initial_state(),
                config={"configurable": {"thread_id": "test_flow_1"}},
            )

        # 验证工具调用顺序：监控优先于知识库
        assert mock_tool_node.call_log == ["query_metrics", "search_knowledge"], \
            f"工具调用顺序错误: {mock_tool_node.call_log}"

        # 验证 tools_used
        assert "query_metrics" in final_state["tools_used"]
        assert "search_knowledge" in final_state["tools_used"]

        # 验证最终回答含诊断报告
        last_msg = final_state["messages"][-1]
        assert "### 现象" in last_msg.content

        # 验证步数（3次 _call_agent）
        assert final_state["step_count"] == 3

    @pytest.mark.asyncio
    async def test_direct_diagnosis_without_tools(self, mock_agent):
        """简单问题无工具调用→直接结束（P2 去反思后）

        LLM 直接回答（无 tool_calls）→ end
        """
        agent, mock_tool_node = mock_agent

        diagnosis = "### 现象\n配置问题\n### 置信度\n中"
        agent.llm_with_tools.invoke = MagicMock(return_value=AIMessage(content=diagnosis))

        with patch("app.langgraph_agent.agent.pop_retrieval_buffer", return_value=[]), \
             patch("app.langgraph_agent.agent.set_conversation_context"), \
             patch("app.langgraph_agent.agent.set_current_user_id"):
            final_state = await agent.graph.ainvoke(
                _make_initial_state("mysql 端口是多少？"),
                config={"configurable": {"thread_id": "test_flow_2"}},
            )

        # 无工具调用
        assert mock_tool_node.call_log == []
        assert final_state["tools_used"] == []

        # 无工具调用直接结束
        assert final_state["step_count"] == 1

        last_msg = final_state["messages"][-1]
        assert "### 现象" in last_msg.content


# ========== max_steps 终止测试 ==========

class TestMaxStepsTermination:
    """最大步数终止测试"""

    @pytest.mark.asyncio
    async def test_max_steps_forces_end(self, mock_agent):
        """达到 max_steps 时强制结束（即使有 tool_calls）"""
        agent, mock_tool_node = mock_agent
        agent.max_steps = 2  # 设置很小的 max_steps
        agent.graph = agent._build_graph()  # 重建图

        # LLM 持续调用工具
        agent.llm_with_tools.invoke = MagicMock(side_effect=[
            AIMessage(content="", tool_calls=[
                _make_tool_call("query_metrics", {"service": "mysql"}, "call_1")
            ]),
            AIMessage(content="", tool_calls=[
                _make_tool_call("query_logs", {"service": "mysql"}, "call_2")
            ]),
            AIMessage(content="", tool_calls=[
                _make_tool_call("search_knowledge", {"query": "x"}, "call_3")
            ]),
        ])

        with patch("app.langgraph_agent.agent.pop_retrieval_buffer", return_value=[]), \
             patch("app.langgraph_agent.agent.set_conversation_context"), \
             patch("app.langgraph_agent.agent.set_current_user_id"):
            final_state = await agent.graph.ainvoke(
                _make_initial_state(),
                config={"configurable": {"thread_id": "test_max_steps"}},
            )

        # step_count 达到 max_steps=2 时 _should_continue 返回 "end"
        # （_call_agent 每次 +1，第2次后 step_count=2 >= max_steps=2 → end）
        assert final_state["step_count"] >= 2


# ========== 悬空 tool_calls 中性化（续跑/重试健壮性）==========

class TestNeutralizeUnpairedToolCalls:
    """未被 ToolMessage 应答的 tool_calls 中性化测试

    背景（R5 error_storm 实测）：诊断跑到 max_steps 被截断时，历史末尾残留带
    tool_calls 的 AIMessage 但无对应 ToolMessage → 重试/续跑同一 thread 恢复
    checkpoint 后原样回传 LLM 触发 OpenAI 400（"assistant message with tool_calls
    must be followed by tool messages"）。_neutralize_unpaired_tool_calls 负责在
    _call_agent 入口中性化这类残缺历史。
    """

    def test_paired_tool_calls_untouched(self):
        """正常配对的 AIMessage(tool_calls)+ToolMessage 不被改动"""
        from app.langgraph_agent.agent import LangGraphAgent

        ai = AIMessage(content="", tool_calls=[
            _make_tool_call("query_metrics", {"service": "mysql"}, "c1")])
        tm = ToolMessage(content='{"ok": true}', tool_call_id="c1")
        out = LangGraphAgent._neutralize_unpaired_tool_calls([ai, tm])
        assert len(out) == 2
        assert out[0].tool_calls  # c1 已被 ToolMessage 应答，保留

    def test_dangling_empty_dropped(self):
        """悬空（无应答）+ 空正文的 tool_calls AIMessage 整条丢弃"""
        from app.langgraph_agent.agent import LangGraphAgent

        ai = AIMessage(content="", tool_calls=[
            _make_tool_call("query_logs", {"service": "mysql"}, "c2")])
        out = LangGraphAgent._neutralize_unpaired_tool_calls([ai])
        assert out == []

    def test_dangling_with_content_strips_tool_calls(self):
        """悬空但带正文的 AIMessage：保留正文、剥离 tool_calls"""
        from app.langgraph_agent.agent import LangGraphAgent

        ai = AIMessage(content="我需要更多证据", tool_calls=[
            _make_tool_call("search_knowledge", {"query": "x"}, "c3")])
        out = LangGraphAgent._neutralize_unpaired_tool_calls([ai])
        assert len(out) == 1
        assert out[0].content == "我需要更多证据"
        assert not out[0].tool_calls

    def test_mid_list_dangling_removed(self):
        """悬空消息夹在历史中间（后续是新一轮 HumanMessage）也被移除"""
        from app.langgraph_agent.agent import LangGraphAgent

        ai_paired = AIMessage(content="", tool_calls=[
            _make_tool_call("query_metrics", {"service": "mysql"}, "c1")])
        tm = ToolMessage(content='{"ok": true}', tool_call_id="c1")
        ai_dangling = AIMessage(content="", tool_calls=[
            _make_tool_call("query_logs", {"service": "mysql"}, "c2")])
        human = HumanMessage(content="继续完成诊断")
        out = LangGraphAgent._neutralize_unpaired_tool_calls(
            [ai_paired, tm, ai_dangling, human])
        assert out == [ai_paired, tm, human]

    @pytest.mark.asyncio
    async def test_resume_after_maxsteps_sends_clean_history(self, mock_agent):
        """max_steps 截断后同 thread resume：LLM 收到的历史不含悬空 tool_calls"""
        agent, _ = mock_agent
        agent.max_steps = 2
        agent.graph = agent._build_graph()

        # run1：LLM 每次都请求工具 → 第 2 次后达 max_steps 终止，留悬空 tool_calls
        agent.llm_with_tools.invoke = MagicMock(side_effect=[
            AIMessage(content="", tool_calls=[
                _make_tool_call("query_metrics", {"service": "mysql"}, "c1")]),
            AIMessage(content="", tool_calls=[
                _make_tool_call("query_logs", {"service": "mysql"}, "c2")]),
        ])
        with patch("app.langgraph_agent.agent.pop_retrieval_buffer", return_value=[]), \
             patch("app.langgraph_agent.agent.set_conversation_context"), \
             patch("app.langgraph_agent.agent.set_current_user_id"):
            await agent.graph.ainvoke(
                _make_initial_state("mysql 连接池为什么耗尽？"),
                config={"configurable": {"thread_id": "test_resume_dangling"}},
            )

        # run2：同一 thread resume，LLM 直接给最终报告
        agent.llm_with_tools.invoke = MagicMock(return_value=AIMessage(content="### 现象\n连接池耗尽\n### 根因分析\n占满"))
        with patch("app.langgraph_agent.agent.pop_retrieval_buffer", return_value=[]), \
             patch("app.langgraph_agent.agent.set_conversation_context"), \
             patch("app.langgraph_agent.agent.set_current_user_id"):
            final_state = await agent.graph.ainvoke(
                _make_initial_state("继续完成诊断"),
                config={"configurable": {"thread_id": "test_resume_dangling"}},
            )

        # 关键断言：resume 的 LLM 调用所收到的历史中，悬空的 c2 已被中性化移除
        sent = agent.llm_with_tools.invoke.call_args[0][0]
        assert not any(
            isinstance(m, AIMessage)
            and any(tc.get("id") == "c2" for tc in (m.tool_calls or []))
            for m in sent
        )
        # 且 resume 最终能正常产出报告（不再抛 400/KeyError）
        text = " ".join(getattr(m, "content", "") or "" for m in final_state["messages"])
        assert "### 现象" in text


# ========== MCP 降级提示集成测试 ==========

class TestMCPDegradationIntegration:
    """MCP 降级提示在系统提示中的集成"""

    def test_system_prompt_includes_mcp_degradation_hint(self, mock_agent):
        """_mcp_status=disabled 时系统提示含降级提示（诊断链路）"""
        agent, _ = mock_agent
        agent._mcp_status = "disabled"

        state = _make_initial_state()
        state["intent"] = "diagnosis"  # MCP 降级提示仅诊断链路注入
        prompt = agent._build_system_prompt(state, user_id="test_user")

        assert "监控工具降级提示" in prompt, "系统提示应含 MCP 降级提示"
        assert "跳过阶段 2A" in prompt, "应引导跳过监控取证"
        assert "中" in prompt, "应限制置信度最高为'中'"
        assert "query_metrics" in prompt, "应禁止调用 query_metrics"

    def test_system_prompt_no_hint_when_mcp_available(self, mock_agent):
        """_mcp_status=success 时系统提示不含降级提示（诊断链路）"""
        agent, _ = mock_agent
        agent._mcp_status = "success"

        state = _make_initial_state()
        state["intent"] = "diagnosis"
        prompt = agent._build_system_prompt(state, user_id="test_user")

        assert "监控工具降级提示" not in prompt, "MCP 可用时不应注入降级提示"

    def test_system_prompt_includes_timeout_reason(self, mock_agent):
        """_mcp_status=timeout 时系统提示含超时原因（诊断链路）"""
        agent, _ = mock_agent
        agent._mcp_status = "timeout"
        agent._mcp_last_error = "MCP 加载超时（>30.0s）"

        state = _make_initial_state()
        state["intent"] = "diagnosis"
        prompt = agent._build_system_prompt(state, user_id="test_user")

        assert "超时" in prompt
        assert "MCP 加载超时（>30.0s）" in prompt


# ========== 监控证据提取测试 ==========

class TestMonitoringEvidenceExtraction:
    """监控证据从 ToolMessage 提取的测试"""

    def test_extract_monitoring_evidence_from_tool_messages(self, mock_agent):
        """_extract_monitoring_evidence 从 query_metrics 的 ToolMessage 提取证据"""
        agent, _ = mock_agent

        # 构造含 tool_calls 的 AIMessage + 对应 ToolMessage
        ai_msg = AIMessage(content="", tool_calls=[
            _make_tool_call("query_metrics", {"service": "mysql"}, "call_1")
        ])
        tool_msg = ToolMessage(
            content=json.dumps({
                "service": "mysql",
                "metrics": {"connection_pool_usage": "100%"},
                "status": "critical",
            }),
            tool_call_id="call_1",
        )

        evidence = _extract_monitoring_evidence([ai_msg, tool_msg])
        assert len(evidence) >= 1
        assert evidence[0]["type"] == "metrics"
        assert evidence[0]["service"] == "mysql"

    def test_extract_evidence_ignores_non_monitoring_tools(self, mock_agent):
        """search_knowledge 的 ToolMessage 不被提取为监控证据"""
        agent, _ = mock_agent

        ai_msg = AIMessage(content="", tool_calls=[
            _make_tool_call("search_knowledge", {"query": "test"}, "call_1")
        ])
        tool_msg = ToolMessage(
            content=json.dumps({"results": []}),
            tool_call_id="call_1",
        )

        evidence = _extract_monitoring_evidence([ai_msg, tool_msg])
        assert len(evidence) == 0, "search_knowledge 不应产生监控证据"
