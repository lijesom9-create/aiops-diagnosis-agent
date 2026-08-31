"""
上下文管理（Token 预算裁剪）验证

测试链路：
1. 短对话：消息数少，不触发裁剪，全部保留
2. 长对话：消息 token 超预算，从最早开始丢弃，保留最近消息
3. tool_calls 配对：裁剪边界不破坏 AIMessage(tool_calls) + ToolMessage 配对
4. SystemMessage 始终保留：裁剪不影响 system prompt
5. _count_message_tokens 正确计算单条消息 token 数
6. _fix_tool_calls_boundary 修复孤立 ToolMessage 和孤立 AIMessage(tool_calls)

用法：
    cd backend
    python evaluation/verify_context.py
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("MONGODB_URL", "mongodb://invalid:27017")
sys.path.insert(0, str(Path(__file__).parent.parent))

print("=" * 70)
print("上下文管理（Token 预算裁剪）验证")
print("=" * 70)

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


# 构造 Agent 实例（mock LLM，不真实调用）
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def make_agent(max_context_tokens=32000, reserved_for_output=4000):
    """构造一个 Agent 实例（mock 掉 LLM 和知识库，只测试裁剪逻辑）"""
    with patch("app.langgraph_agent.agent.ChatOpenAI"), \
         patch("app.langgraph_agent.agent.ToolNode"), \
         patch("app.langgraph_agent.agent.create_tools", return_value=[]), \
         patch("app.langgraph_agent.agent.set_knowledge_store"):
        from app.langgraph_agent.agent import LangGraphAgent
        agent = LangGraphAgent(
            llm_model="deepseek-chat",
            llm_base_url="http://dummy",
            llm_api_key="dummy",
            knowledge_store=MagicMock(),
            checkpoint_path=None,  # 用 MemorySaver，不走 SQLite
            max_context_tokens=max_context_tokens,
            reserved_for_output=reserved_for_output,
        )
    return agent


# ============================================================
# 1. _count_message_tokens 单条消息 token 计数
# ============================================================
print("\n【测试 1】_count_message_tokens 单条消息 token 计数")
print("-" * 70)

agent = make_agent()

# 1.1 空消息
tokens_empty = agent._count_message_tokens(HumanMessage(content=""))
check("空消息 token 数 >= 4（overhead）", tokens_empty >= 4, f"实际: {tokens_empty}")

# 1.2 短消息
tokens_short = agent._count_message_tokens(HumanMessage(content="你好"))
check("短消息 token 数 > 4", tokens_short > 4, f"实际: {tokens_short}")

# 1.3 长消息 token 数大于短消息
tokens_long = agent._count_message_tokens(HumanMessage(content="这是一段很长的文本内容" * 100))
check("长消息 token > 短消息 token", tokens_long > tokens_short, f"长:{tokens_long} 短:{tokens_short}")

# 1.4 SystemMessage
tokens_sys = agent._count_message_tokens(SystemMessage(content="你是一个助手"))
check("SystemMessage token 计数正常", tokens_sys > 4, f"实际: {tokens_sys}")


# ============================================================
# 2. 短对话不裁剪
# ============================================================
print("\n【测试 2】短对话不裁剪（消息 token 远低于预算）")
print("-" * 70)

agent = make_agent(max_context_tokens=32000, reserved_for_output=4000)

short_messages = [
    SystemMessage(content="你是一个助手"),
    HumanMessage(content="你好"),
    AIMessage(content="你好！有什么可以帮你的？"),
    HumanMessage(content="FastAPI 是什么？"),
    AIMessage(content="FastAPI 是一个现代的 Web 框架。"),
]

trimmed = agent._trim_messages_to_budget(short_messages)
check(
    "短对话全部保留",
    len(trimmed) == len(short_messages),
    f"原始: {len(short_messages)}, 裁剪后: {len(trimmed)}",
)
check("SystemMessage 保留", any(isinstance(m, SystemMessage) for m in trimmed))
check("最后一条消息保留", isinstance(trimmed[-1], AIMessage))


# ============================================================
# 3. 长对话触发裁剪
# ============================================================
print("\n【测试 3】长对话触发裁剪（token 超预算）")
print("-" * 70)

# 用很小的预算，强制触发裁剪
agent_small = make_agent(max_context_tokens=500, reserved_for_output=100)
# 实际预算 = 500 - 100 = 400 tokens

# 构造 20 条长消息（每条约 50+ tokens）
long_messages = [SystemMessage(content="你是一个助手")]  # system prompt
for i in range(20):
    long_messages.append(HumanMessage(content=f"这是第 {i+1} 轮对话，" + "内容内容内容" * 10))
    long_messages.append(AIMessage(content=f"这是第 {i+1} 轮回答，" + "回答回答回答" * 10))

original_count = len(long_messages)
trimmed = agent_small._trim_messages_to_budget(long_messages)
trimmed_count = len(trimmed)

check(
    "长对话触发裁剪",
    trimmed_count < original_count,
    f"原始: {original_count}, 裁剪后: {trimmed_count}",
)
check("裁剪后 SystemMessage 保留", any(isinstance(m, SystemMessage) for m in trimmed))
check(
    "裁剪后保留最后一条消息",
    trimmed[-1] is long_messages[-1],
    "最后一条消息应被保留",
)

# 验证裁剪后 token 总数在预算内
total_tokens = sum(agent_small._count_message_tokens(m) for m in trimmed)
budget = agent_small.max_context_tokens  # 500
check(
    "裁剪后 token 总数 <= 预算",
    total_tokens <= budget,
    f"实际: {total_tokens}, 预算: {budget}",
)


# ============================================================
# 4. tool_calls 配对完整性
# ============================================================
print("\n【测试 4】tool_calls 配对完整性")
print("-" * 70)

# 构造含 tool_calls 的消息序列
# AIMessage(tool_calls) → ToolMessage → AIMessage(回答)
tc_id = "call_abc123"
messages_with_tools = [
    SystemMessage(content="你是一个助手"),
    HumanMessage(content="搜索 FastAPI"),
    AIMessage(
        content="",
        tool_calls=[{"id": tc_id, "name": "search_knowledge", "args": {"query": "FastAPI"}}],
    ),
    ToolMessage(content="FastAPI 是一个 Web 框架", tool_call_id=tc_id),
    AIMessage(content="根据搜索结果，FastAPI 是一个现代的 Web 框架。"),
]

# 4.1 正常情况（不裁剪）：配对完整
agent_normal = make_agent(max_context_tokens=32000, reserved_for_output=4000)
trimmed = agent_normal._trim_messages_to_budget(messages_with_tools)
check(
    "不裁剪时 tool_calls 配对完整",
    any(isinstance(m, AIMessage) and m.tool_calls for m in trimmed)
    and any(isinstance(m, ToolMessage) and m.tool_call_id == tc_id for m in trimmed),
)

# 4.2 裁剪后开头不能是孤立的 ToolMessage
# 构造裁剪后会从 ToolMessage 开头的场景
agent_tiny = make_agent(max_context_tokens=300, reserved_for_output=50)
# 添加很多消息，让裁剪发生在 ToolMessage 附近
many_messages = [SystemMessage(content="你是一个助手")]
# 前面填充大量消息（会被裁掉）
for i in range(15):
    many_messages.append(HumanMessage(content=f"第{i+1}轮问题" + "填充" * 20))
    many_messages.append(AIMessage(content=f"第{i+1}轮回答" + "填充" * 20))
# 后面是 tool_calls 序列
many_messages.append(HumanMessage(content="搜索一下"))
many_messages.append(AIMessage(
    content="",
    tool_calls=[{"id": "tc_001", "name": "search", "args": {"q": "test"}}],
))
many_messages.append(ToolMessage(content="搜索结果", tool_call_id="tc_001"))
many_messages.append(AIMessage(content="根据搜索结果回答"))

trimmed = agent_tiny._trim_messages_to_budget(many_messages)

# 检查裁剪后第一条非 System 消息不是孤立的 ToolMessage
first_non_system = next((m for m in trimmed if not isinstance(m, SystemMessage)), None)
if first_non_system:
    check(
        "裁剪后开头不是孤立 ToolMessage",
        not isinstance(first_non_system, ToolMessage),
        f"第一条: {type(first_non_system).__name__}",
    )
else:
    check("裁剪后开头不是孤立 ToolMessage", False, "无非 system 消息")

# 4.3 裁剪后开头不能是孤立的 AIMessage(tool_calls)（没有对应 ToolMessage）
# 检查是否有 tool_calls 的 AIMessage 后面跟着对应的 ToolMessage
has_orphan_ai_tc = False
for i, m in enumerate(trimmed):
    if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
        # 检查后面是否有对应的 ToolMessage
        tc_ids = {tc.get("id") for tc in m.tool_calls if tc.get("id")}
        has_response = any(
            isinstance(tm, ToolMessage) and tm.tool_call_id in tc_ids
            for tm in trimmed[i+1:i+6]
        )
        if not has_response:
            has_orphan_ai_tc = True
            break

check(
    "裁剪后无孤立的 AIMessage(tool_calls)",
    not has_orphan_ai_tc,
    "存在没有对应 ToolMessage 的 AIMessage(tool_calls)",
)


# ============================================================
# 5. _fix_tool_calls_boundary 单独测试
# ============================================================
print("\n【测试 5】_fix_tool_calls_boundary 边界修复")
print("-" * 70)

agent_fix = make_agent()

# 5.1 移除开头孤立的 ToolMessage
tc_id2 = "call_xyz"
messages_isolated_tool = [
    ToolMessage(content="孤立工具结果", tool_call_id=tc_id2),
    AIMessage(content="回答"),
    HumanMessage(content="下一个问题"),
]
fixed = agent_fix._fix_tool_calls_boundary(messages_isolated_tool)
check(
    "移除开头孤立 ToolMessage",
    not isinstance(fixed[0], ToolMessage),
    f"第一条: {type(fixed[0]).__name__ if fixed else '空'}",
)

# 5.2 移除开头孤立的 AIMessage(tool_calls)
messages_isolated_ai = [
    AIMessage(content="", tool_calls=[{"id": "tc_orphan", "name": "search", "args": {}}]),
    HumanMessage(content="下一个问题"),
    AIMessage(content="回答"),
]
fixed = agent_fix._fix_tool_calls_boundary(messages_isolated_ai)
check(
    "移除开头孤立 AIMessage(tool_calls)",
    not (isinstance(fixed[0], AIMessage) and getattr(fixed[0], "tool_calls", None)),
    f"第一条: {type(fixed[0]).__name__ if fixed else '空'}",
)

# 5.3 保留完整的 tool_calls 配对
messages_paired = [
    AIMessage(content="", tool_calls=[{"id": "tc_ok", "name": "search", "args": {}}]),
    ToolMessage(content="结果", tool_call_id="tc_ok"),
    AIMessage(content="回答"),
]
fixed = agent_fix._fix_tool_calls_boundary(messages_paired)
check(
    "保留完整 tool_calls 配对",
    len(fixed) == 3,
    f"原始: 3, 修复后: {len(fixed)}",
)

# 5.4 空列表
fixed_empty = agent_fix._fix_tool_calls_boundary([])
check("空列表修复后仍为空", len(fixed_empty) == 0)


# ============================================================
# 6. SystemMessage 始终保留
# ============================================================
print("\n【测试 6】SystemMessage 始终保留（即使预算很小）")
print("-" * 70)

agent_small2 = make_agent(max_context_tokens=200, reserved_for_output=50)

messages_with_sys = [
    SystemMessage(content="你是一个智能助手，请用中文回答用户问题"),
    HumanMessage(content="你好" * 50),
    AIMessage(content="你好" * 50),
    HumanMessage(content="再问一次" * 50),
]

trimmed = agent_small2._trim_messages_to_budget(messages_with_sys)
check(
    "小预算下 SystemMessage 仍保留",
    any(isinstance(m, SystemMessage) for m in trimmed),
    "SystemMessage 应始终保留",
)
check(
    "小预算下至少保留一条非 system 消息",
    any(not isinstance(m, SystemMessage) for m in trimmed),
)


# ============================================================
# 7. 多轮 tool_calls 序列裁剪
# ============================================================
print("\n【测试 7】多轮 tool_calls 序列裁剪")
print("-" * 70)

# 构造两轮完整的 tool_calls 序列
messages_multi_round = [
    SystemMessage(content="你是一个助手"),
    # 第一轮
    HumanMessage(content="搜索 FastAPI"),
    AIMessage(content="", tool_calls=[
        {"id": "tc_1", "name": "search", "args": {"q": "FastAPI"}}
    ]),
    ToolMessage(content="FastAPI 结果", tool_call_id="tc_1"),
    AIMessage(content="FastAPI 是一个框架"),
    # 第二轮
    HumanMessage(content="再搜索 Django"),
    AIMessage(content="", tool_calls=[
        {"id": "tc_2", "name": "search", "args": {"q": "Django"}}
    ]),
    ToolMessage(content="Django 结果", tool_call_id="tc_2"),
    AIMessage(content="Django 也是一个框架"),
]

# 用中等预算，只保留第二轮
agent_mid = make_agent(max_context_tokens=400, reserved_for_output=100)
trimmed = agent_mid._trim_messages_to_budget(messages_multi_round)

# 验证保留的第二轮 tool_calls 配对完整
has_tc2 = any(
    isinstance(m, AIMessage) and m.tool_calls
    and any(tc.get("id") == "tc_2" for tc in m.tool_calls)
    for m in trimmed
)
has_tool_msg_2 = any(
    isinstance(m, ToolMessage) and m.tool_call_id == "tc_2"
    for m in trimmed
)
check(
    "保留的第二轮 AIMessage(tool_calls) + ToolMessage 配对完整",
    has_tc2 and has_tool_msg_2,
    f"有 tc_2 的 AIMessage: {has_tc2}, 有 tc_2 的 ToolMessage: {has_tool_msg_2}",
)


# ============================================================
# 汇总
# ============================================================
print("\n" + "=" * 70)
print(f"验证结果：{passed} 通过，{failed} 失败")
print("=" * 70)
sys.exit(0 if failed == 0 else 1)
