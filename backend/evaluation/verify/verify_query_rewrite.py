"""
多轮对话查询重写（指代消解）验证

测试链路：
1. set_conversation_context：正确提取最近 6 条消息文本
2. _needs_query_rewrite：规则判断指代词/省略语
3. _rewrite_query_with_context 三级判断：
   a. 无对话上下文 → 跳过
   b. 无指代词 → 跳过
   c. 有指代词 + 有上下文 → LLM 重写
4. LLM 失败静默降级：返回原查询
5. 缓存命中：相同 query + context 不重复调用 LLM
6. search_knowledge 工具集成：检索前调用重写

用法：
    cd backend
    python evaluation/verify_query_rewrite.py
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("MONGODB_URL", "mongodb://invalid:27017")
sys.path.insert(0, str(Path(__file__).parent.parent))

print("=" * 70)
print("多轮对话查询重写（指代消解）验证")
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


from langchain_core.messages import AIMessage, HumanMessage

# ============================================================
# 1. set_conversation_context 正确提取对话文本
# ============================================================
print("\n【测试 1】set_conversation_context 正确提取对话文本")
print("-" * 70)

from app.langgraph_agent import tools as tools_module

# 1.1 正常对话历史
messages = [
    HumanMessage(content="FastAPI 是什么？"),
    AIMessage(content="FastAPI 是一个现代的 Web 框架。"),
    HumanMessage(content="它的路由怎么定义？"),
]
tools_module.set_conversation_context(messages)
ctx = tools_module._conversation_context
check("提取 3 条消息", len(ctx) == 3, f"实际: {len(ctx)}")
check("第一条是 user 角色", ctx[0].startswith("user:"), f"实际: {ctx[0][:20]}")
check("第二条是 assistant 角色", ctx[1].startswith("assistant:"), f"实际: {ctx[1][:20]}")
check("第三条是 user 角色", ctx[2].startswith("user:"), f"实际: {ctx[2][:20]}")

# 1.2 空消息列表
tools_module.set_conversation_context([])
check("空消息列表 → 上下文为空", len(tools_module._conversation_context) == 0)

# 1.3 超过 6 条只取最近 6 条
long_messages = [HumanMessage(content=f"消息{i}") for i in range(10)]
tools_module.set_conversation_context(long_messages)
check("超过 6 条只取最近 6 条", len(tools_module._conversation_context) == 6, f"实际: {len(tools_module._conversation_context)}")

# 1.4 None 安全处理
tools_module.set_conversation_context(None)
check("None 安全处理", len(tools_module._conversation_context) == 0)


# ============================================================
# 2. _needs_query_rewrite 规则判断
# ============================================================
print("\n【测试 2】_needs_query_rewrite 规则判断指代词")
print("-" * 70)

# 2.1 含指代词 → 需要重写
check("'它的路由' 需要重写", tools_module._needs_query_rewrite("它的路由怎么定义？"))
check("'这个框架' 需要重写", tools_module._needs_query_rewrite("这个框架怎么用？"))
check("'那个功能' 需要重写", tools_module._needs_query_rewrite("那个功能是什么？"))
check("'继续' 需要重写", tools_module._needs_query_rewrite("继续"))
check("'原理是什么' 需要重写", tools_module._needs_query_rewrite("原理是什么"))

# 2.2 不含指代词 → 不需要重写
check("'FastAPI 路由定义' 不需要重写", not tools_module._needs_query_rewrite("FastAPI 路由定义"))
check("'Python 异步编程' 不需要重写", not tools_module._needs_query_rewrite("Python 异步编程指南"))
check("'Django 和 Flask 的区别' 不需要重写", not tools_module._needs_query_rewrite("Django 和 Flask 的区别"))

# 2.3 过短查询需要重写（可能是追问）
check("'嗯' 需要重写（过短）", tools_module._needs_query_rewrite("嗯"))
check("空查询不需要重写", not tools_module._needs_query_rewrite(""))


# ============================================================
# 3. _rewrite_query_with_context 三级判断
# ============================================================
print("\n【测试 3】_rewrite_query_with_context 三级判断")
print("-" * 70)

# 3.1 第一级：无对话上下文 → 跳过
tools_module._conversation_context = []
result = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check("无上下文时返回原查询", result == "它的路由怎么定义？", f"实际: {result}")

# 3.2 第二级：有上下文但无指代词 → 跳过
tools_module.set_conversation_context([
    HumanMessage(content="FastAPI 是什么？"),
    AIMessage(content="FastAPI 是一个 Web 框架。"),
])
result = tools_module._rewrite_query_with_context("FastAPI 路由定义")
check("有上下文但无指代词返回原查询", result == "FastAPI 路由定义", f"实际: {result}")

# 3.3 第三级：有上下文 + 有指代词 → LLM 重写
tools_module.set_conversation_context([
    HumanMessage(content="FastAPI 是什么？"),
    AIMessage(content="FastAPI 是一个现代的 Web 框架，用于构建 API。"),
])

# Mock LLM
mock_llm = MagicMock()
mock_response = MagicMock()
mock_response.content = "FastAPI 的路由怎么定义？"
mock_llm.invoke = MagicMock(return_value=mock_response)
tools_module.set_query_rewriter_llm(mock_llm)

# 清空缓存
tools_module._query_rewrite_cache.clear()

result = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check(
    "有指代词时 LLM 重写查询",
    result == "FastAPI 的路由怎么定义？",
    f"实际: {result}",
)
check("LLM 被调用一次", mock_llm.invoke.call_count == 1)


# ============================================================
# 4. LLM 失败静默降级
# ============================================================
print("\n【测试 4】LLM 失败静默降级")
print("-" * 70)

tools_module._query_rewrite_cache.clear()

# 4.1 LLM 抛异常 → 返回原查询
mock_llm_fail = MagicMock()
mock_llm_fail.invoke = MagicMock(side_effect=Exception("LLM 不可用"))
tools_module.set_query_rewriter_llm(mock_llm_fail)

result = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check(
    "LLM 异常时返回原查询",
    result == "它的路由怎么定义？",
    f"实际: {result}",
)

# 4.2 LLM 返回空内容 → 返回原查询
mock_llm_empty = MagicMock()
mock_response_empty = MagicMock()
mock_response_empty.content = ""
mock_llm_empty.invoke = MagicMock(return_value=mock_response_empty)
tools_module.set_query_rewriter_llm(mock_llm_empty)
tools_module._query_rewrite_cache.clear()

result = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check("LLM 返回空时返回原查询", result == "它的路由怎么定义？", f"实际: {result}")

# 4.3 LLM 返回与原查询相同 → 返回原查询
mock_llm_same = MagicMock()
mock_response_same = MagicMock()
mock_response_same.content = "它的路由怎么定义？"
mock_llm_same.invoke = MagicMock(return_value=mock_response_same)
tools_module.set_query_rewriter_llm(mock_llm_same)
tools_module._query_rewrite_cache.clear()

result = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check("LLM 返回相同时用原查询", result == "它的路由怎么定义？", f"实际: {result}")

# 4.4 LLM 未初始化 → 返回原查询
tools_module._query_rewriter_llm = None
tools_module._query_rewrite_cache.clear()
result = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check("LLM 未初始化时返回原查询", result == "它的路由怎么定义？", f"实际: {result}")


# ============================================================
# 5. 缓存命中
# ============================================================
print("\n【测试 5】缓存命中（相同 query + context 不重复调用 LLM）")
print("-" * 70)

# 重新设置有指代词的上下文
tools_module.set_conversation_context([
    HumanMessage(content="FastAPI 是什么？"),
    AIMessage(content="FastAPI 是一个现代的 Web 框架。"),
])

mock_llm_cache = MagicMock()
mock_resp = MagicMock()
mock_resp.content = "FastAPI 的路由怎么定义？"
mock_llm_cache.invoke = MagicMock(return_value=mock_resp)
tools_module.set_query_rewriter_llm(mock_llm_cache)
tools_module._query_rewrite_cache.clear()

# 第一次调用：LLM 被调用
result1 = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check("第一次调用 LLM 重写", result1 == "FastAPI 的路由怎么定义？")
check("第一次调用 LLM 被调用 1 次", mock_llm_cache.invoke.call_count == 1)

# 第二次相同调用：应命中缓存
result2 = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check("第二次调用结果相同", result2 == result1)
check("第二次调用 LLM 仍为 1 次（缓存命中）", mock_llm_cache.invoke.call_count == 1)

# 5.2 不同上下文 → 不命中缓存，重新调用 LLM
tools_module.set_conversation_context([
    HumanMessage(content="Django 是什么？"),
    AIMessage(content="Django 是一个 Python Web 框架。"),
])
result3 = tools_module._rewrite_query_with_context("它的路由怎么定义？")
check("不同上下文不命中缓存", mock_llm_cache.invoke.call_count == 2)


# ============================================================
# 6. search_knowledge 工具集成
# ============================================================
print("\n【测试 6】search_knowledge 工具集成（检索前调用重写）")
print("-" * 70)

# 6.1 无上下文时，search_knowledge 不触发重写，直接检索
tools_module._conversation_context = []
tools_module._query_rewrite_cache.clear()
tools_module._tool_cache.clear()

# Mock knowledge_store
mock_store = MagicMock()
mock_store.hybrid_search_parent_child = MagicMock(return_value=[
    {"title": "FastAPI 路由", "content": "路由定义内容", "score": 0.9, "metadata": {}}
])
tools_module.set_knowledge_store(mock_store)

text = tools_module.search_knowledge.invoke({"query": "FastAPI 路由", "limit": 5})
check("无上下文时直接检索", mock_store.hybrid_search_parent_child.call_count == 1)
# 验证传入的 query 是原始 query
call_args = mock_store.hybrid_search_parent_child.call_args
check("无上下文时用原查询检索", call_args[0][0] == "FastAPI 路由", f"实际: {call_args[0][0]}")
# 清空 buffer，避免影响后续测试
tools_module.pop_retrieval_buffer()

# 6.2 有上下文 + 有指代词时，search_knowledge 触发重写
tools_module._tool_cache.clear()
mock_store.hybrid_search_parent_child.reset_mock()

tools_module.set_conversation_context([
    HumanMessage(content="FastAPI 是什么？"),
    AIMessage(content="FastAPI 是一个 Web 框架。"),
])
mock_llm_integrate = MagicMock()
mock_resp_integrate = MagicMock()
mock_resp_integrate.content = "FastAPI 的路由怎么定义？"
mock_llm_integrate.invoke = MagicMock(return_value=mock_resp_integrate)
tools_module.set_query_rewriter_llm(mock_llm_integrate)
tools_module._query_rewrite_cache.clear()

text = tools_module.search_knowledge.invoke({"query": "它的路由怎么定义？", "limit": 5})
check("有指代词时触发检索", mock_store.hybrid_search_parent_child.call_count == 1)
call_args = mock_store.hybrid_search_parent_child.call_args
check(
    "有指代词时用重写后的查询检索",
    call_args[0][0] == "FastAPI 的路由怎么定义？",
    f"实际: {call_args[0][0]}",
)

# 6.3 验证返回文本带编号
check("返回文本带编号", "[1]" in text, f"实际文本: {text[:100]}")

# 6.4 验证 buffer 写入
buffered = tools_module.pop_retrieval_buffer()
check("buffer 写入 artifact", len(buffered) == 1, f"实际: {len(buffered)}")
check("artifact 含 doc_id", "doc_id" in buffered[0] if buffered else False)


# ============================================================
# 7. 完整多轮对话场景模拟
# ============================================================
print("\n【测试 7】完整多轮对话场景模拟")
print("-" * 70)

# 场景：
# 第1轮：用户问"FastAPI 是什么" → 无上下文，不重写
# 第2轮：用户问"它的路由怎么定义" → 有上下文，重写为"FastAPI 的路由怎么定义"
# 第3轮：用户问"再说说依赖注入" → 有上下文，重写为"FastAPI 的依赖注入"

mock_store.reset_mock()
tools_module._tool_cache.clear()
tools_module._query_rewrite_cache.clear()

mock_llm_scene = MagicMock()
def scene_rewrite_response(messages):
    """根据输入的完整 prompt 内容返回不同的重写结果"""
    # messages[-1] 是 HumanMessage，content 是完整的重写 prompt
    # 重写 prompt 末尾有 "用户追问：{query}" 行，提取实际查询
    prompt_content = messages[-1].content
    # 从 prompt 中提取 "用户追问：" 后的实际查询
    actual_query = ""
    for line in prompt_content.split("\n"):
        if line.startswith("用户追问："):
            actual_query = line.replace("用户追问：", "").strip()
            break

    if "再说说依赖注入" in actual_query:
        resp = MagicMock()
        resp.content = "FastAPI 的依赖注入机制"
        return resp
    if "它的路由" in actual_query:
        resp = MagicMock()
        resp.content = "FastAPI 的路由怎么定义？"
        return resp
    resp = MagicMock()
    resp.content = actual_query or prompt_content
    return resp

mock_llm_scene.invoke = MagicMock(side_effect=scene_rewrite_response)
tools_module.set_query_rewriter_llm(mock_llm_scene)

# 第1轮：无上下文
tools_module.set_conversation_context([])
text1 = tools_module.search_knowledge.invoke({"query": "FastAPI 是什么", "limit": 5})
call1 = mock_store.hybrid_search_parent_child.call_args
check("第1轮用原查询", call1[0][0] == "FastAPI 是什么", f"实际: {call1[0][0]}")

# 第2轮：有上下文 + 指代词
tools_module.set_conversation_context([
    HumanMessage(content="FastAPI 是什么"),
    AIMessage(content="FastAPI 是一个 Web 框架"),
])
tools_module._tool_cache.clear()
text2 = tools_module.search_knowledge.invoke({"query": "它的路由怎么定义", "limit": 5})
call2 = mock_store.hybrid_search_parent_child.call_args
check(
    "第2轮指代消解：'它的路由怎么定义' → 'FastAPI 的路由怎么定义？'",
    call2[0][0] == "FastAPI 的路由怎么定义？",
    f"实际: {call2[0][0]}",
)

# 第3轮：有上下文 + 省略语
tools_module.set_conversation_context([
    HumanMessage(content="FastAPI 是什么"),
    AIMessage(content="FastAPI 是一个 Web 框架"),
    HumanMessage(content="它的路由怎么定义"),
    AIMessage(content="FastAPI 路由用 @app.get 装饰器定义"),
])
tools_module._tool_cache.clear()
text3 = tools_module.search_knowledge.invoke({"query": "再说说依赖注入", "limit": 5})
call3 = mock_store.hybrid_search_parent_child.call_args
check(
    "第3轮省略补全：'再说说依赖注入' → 'FastAPI 的依赖注入机制'",
    call3[0][0] == "FastAPI 的依赖注入机制",
    f"实际: {call3[0][0]}",
)


# ============================================================
# 汇总
# ============================================================
print("\n" + "=" * 70)
print(f"验证结果：{passed} 通过，{failed} 失败")
print("=" * 70)
sys.exit(0 if failed == 0 else 1)
