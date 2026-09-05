"""
引用溯源（Citation）端到端验证

测试链路：
1. search_knowledge 工具返回带编号文本 + 写入模块级 buffer
2. pop_retrieval_buffer() 读取结构化数据
3. _merge_retrieved_docs 去重合并
4. _build_citations 生成最终引用列表

用法：
    cd backend
    python evaluation/verify_citations.py
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).parent.parent))

print("=" * 70)
print("引用溯源（Citation）端到端验证")
print("=" * 70)


# ============================================================
# 1. 测试 search_knowledge 返回文本 + 写入 buffer
# ============================================================
print("\n【测试 1】search_knowledge 返回带编号文本 + 写入 buffer")
print("-" * 70)

from app.langgraph_agent.tools import (
    _tool_cache,
    pop_retrieval_buffer,
    search_knowledge,
    set_knowledge_store,
)

# 清空缓存和 buffer
_tool_cache.clear()
with __import__('contextlib').suppress(Exception):
    pop_retrieval_buffer()

# Mock 知识库
mock_store = MagicMock()
mock_store.hybrid_search_parent_child.return_value = [
    {
        "id": "child_001",
        "content": "FastAPI 是一个现代、快速（高性能）的 Web 框架，基于标准 Python 类型提示。",
        "score": 0.89,
        "title": "第一章_FastAPI入门",
        "metadata": {
            "document_id": "real_第一章_FastAPI入门_123",
            "heading_path": ["第一章", "1.1 FastAPI 简介"],
            "heading_path_str": "第一章 > 1.1 FastAPI 简介",
        },
    },
    {
        "id": "child_002",
        "content": "路由装饰器：@app.get('/items/{item_id}')，支持路径参数和查询参数。",
        "score": 0.82,
        "title": "第二章_FastAPI 进阶",
        "metadata": {
            "document_id": "real_第二章_FastAPI_进阶_456",
            "heading_path": ["第二章", "2.1 路由定义"],
            "heading_path_str": "第二章 > 2.1 路由定义",
            "image_path": "images/route_demo.png",
        },
    },
]
set_knowledge_store(mock_store)

# 调用工具
text = search_knowledge.invoke({"query": "FastAPI 路由", "limit": 5})

# 验证返回是字符串
assert isinstance(text, str), f"应返回字符串，实际 {type(text)}"
print(f"  返回类型: {type(text).__name__}")
print(f"  文本长度: {len(text)} 字符")
print("\n  文本前 200 字符:")
print("  ---")
print(f"  {text[:200]}")
print("  ---")

# 验证文本带 [1][2] 编号
assert "[1]" in text, "文本应包含 [1] 编号"
assert "[2]" in text, "文本应包含 [2] 编号"
assert "请在回答中使用 [1]、[2] 等编号引用上述来源" in text, "文本应提示 LLM 使用引用编号"
print("\n  ✓ 文本带 [1][2] 编号 + 引用提示")

# 验证 buffer 已写入结构化数据
buffered = pop_retrieval_buffer()
assert len(buffered) == 2, f"buffer 应有 2 条，实际 {len(buffered)}"
print(f"\n  buffer 结构化数据: {len(buffered)} 条")

# 验证 artifact 结构
assert buffered[0]["doc_id"] == "real_第一章_FastAPI入门_123"
assert buffered[0]["title"] == "第一章_FastAPI入门"
assert buffered[0]["heading_path"] == "第一章 > 1.1 FastAPI 简介"
assert buffered[0]["score"] == 0.89
assert buffered[0]["source"] == "knowledge_base"
assert buffered[1]["image_path"] == "images/route_demo.png", "第二条应有 image_path"
print("  ✓ 结构正确: index/doc_id/title/heading_path/score/source/image_path")

# 验证 buffer 读取后已清空
assert len(pop_retrieval_buffer()) == 0, "buffer 读取后应清空"
print("  ✓ buffer 读取后已清空")

# 验证空结果
_tool_cache.clear()
pop_retrieval_buffer()
mock_store_empty = MagicMock()
mock_store_empty.hybrid_search_parent_child.return_value = []
set_knowledge_store(mock_store_empty)
empty_text = search_knowledge.invoke({"query": "不存在的内容", "limit": 5})
assert empty_text == "未找到相关知识"
assert len(pop_retrieval_buffer()) == 0, "空结果不应写入 buffer"
print("  ✓ 空结果不写入 buffer")

print("\n  ✓ 测试 1 通过")


# ============================================================
# 2. 测试缓存命中时重放 artifact 到 buffer
# ============================================================
print("\n【测试 2】缓存命中时重放 artifact 到 buffer")
print("-" * 70)

# 恢复 mock
set_knowledge_store(mock_store)
_tool_cache.clear()
pop_retrieval_buffer()

# 第一次调用（填充缓存）
text1 = search_knowledge.invoke({"query": "FastAPI 缓存测试", "limit": 3})
buffered1 = pop_retrieval_buffer()
assert len(buffered1) == 2, "首次调用应有 2 条结果"
print(f"  首次调用: text={len(text1)}字符, buffer={len(buffered1)}条")

# 第二次调用（命中缓存）
text2 = search_knowledge.invoke({"query": "FastAPI 缓存测试", "limit": 3})
buffered2 = pop_retrieval_buffer()
assert text2 == text1, "缓存命中应返回相同文本"
assert len(buffered2) == 2, "缓存命中也应重放 artifact 到 buffer"
print(f"  缓存命中: text={len(text2)}字符, buffer={len(buffered2)}条")

print("\n  ✓ 测试 2 通过")


# ============================================================
# 3. 测试 _merge_retrieved_docs 去重
# ============================================================
print("\n【测试 3】_merge_retrieved_docs 去重")
print("-" * 70)

from app.langgraph_agent.evidence import _build_citations, _merge_retrieved_docs

# 构造有重复的检索结果
docs_with_dupes = [
    {"doc_id": "doc_a", "title": "A", "score": 0.9, "content": "内容A..."},
    {"doc_id": "doc_b", "title": "B", "score": 0.85, "content": "内容B..."},
    {"doc_id": "doc_a", "title": "A", "score": 0.9, "content": "内容A..."},  # 重复
    {"doc_id": "doc_c", "title": "C", "score": 0.7, "content": "内容C..."},
]
merged = _merge_retrieved_docs(docs_with_dupes)
assert len(merged) == 3, f"去重后应为 3 条，实际 {len(merged)}"
print(f"  原始 {len(docs_with_dupes)} 条 -> 去重后 {len(merged)} 条")
print("  ✓ 去重正确")


# ============================================================
# 4. 测试 _build_citations 去重 + 排序 + 编号
# ============================================================
print("\n【测试 4】_build_citations 去重 + 排序 + 编号")
print("-" * 70)

raw_docs = [
    {"index": 1, "doc_id": "doc_a", "title": "文档A", "heading_path": "第一章", "score": 0.85, "content": "内容A...", "source": "knowledge_base", "image_path": None},
    {"index": 1, "doc_id": "doc_b", "title": "文档B", "heading_path": "第二章", "score": 0.92, "content": "内容B...", "source": "knowledge_base", "image_path": "img1.png"},
    {"index": 2, "doc_id": "doc_a", "title": "文档A", "heading_path": "第一章", "score": 0.85, "content": "内容A...", "source": "knowledge_base", "image_path": None},  # 重复
    {"index": 1, "doc_id": "doc_c", "title": "文档C", "heading_path": "第三章", "score": 0.78, "content": "内容C...", "source": "knowledge_base", "image_path": None},
]

citations = _build_citations(raw_docs)

print(f"  原始 {len(raw_docs)} 条 -> 去重后 {len(citations)} 条")
for c in citations:
    print(f"    [{c['index']}] score={c['score']:.2f} | {c['title']} | {c['heading_path']} | img={c['image_path']}")

# 验证去重
assert len(citations) == 3, f"去重后应为 3 条，实际 {len(citations)}"
# 验证按分数降序排序
assert citations[0]["score"] >= citations[1]["score"] >= citations[2]["score"], "应按分数降序"
assert citations[0]["doc_id"] == "doc_b", f"最高分应为 doc_b (0.92)，实际 {citations[0]['doc_id']}"
# 验证重新编号
assert citations[0]["index"] == 1
assert citations[1]["index"] == 2
assert citations[2]["index"] == 3
# 验证精简字段
assert "content" not in citations[0], "citations 不应包含 content"
# 验证图片引用保留
img_citations = [c for c in citations if c["image_path"]]
assert len(img_citations) == 1, "应有 1 条带 image_path"
assert img_citations[0]["image_path"] == "img1.png"

print(f"\n  ✓ 去重: {len(raw_docs)} -> {len(citations)} 条")
print(f"  ✓ 排序: 按分数降序 ({[c['score'] for c in citations]})")
print(f"  ✓ 编号: 重新从 1 开始 ({[c['index'] for c in citations]})")
print("  ✓ 精简: 不含 content 字段")
print(f"  ✓ 图片引用: {len(img_citations)} 条带 image_path")

# 测试空输入
assert _build_citations([]) == []
print("  ✓ 空输入返回空列表")

print("\n  ✓ 测试 4 通过")


# ============================================================
# 5. 模拟 Agent 流程：buffer → _call_agent → citations
# ============================================================
print("\n【测试 5】模拟 Agent 流程：buffer → retrieved_docs → citations")
print("-" * 70)

# 模拟 Agent 多轮工具调用
# 第 1 轮：search_knowledge 写入 buffer
set_knowledge_store(mock_store)
_tool_cache.clear()
pop_retrieval_buffer()  # 清空
search_knowledge.invoke({"query": "FastAPI", "limit": 3})

# 模拟 _call_agent 第 1 次调用（工具执行后）
buffered_round1 = pop_retrieval_buffer()
retrieved_after_round1 = _merge_retrieved_docs(buffered_round1)
print(f"  第 1 轮工具调用后: retrieved_docs={len(retrieved_after_round1)} 条")

# 第 2 轮：再次 search_knowledge（不同查询）
_tool_cache.clear()
mock_store.hybrid_search_parent_child.return_value = [
    {
        "id": "child_003",
        "content": "FastAPI 自动校验基于 Pydantic 模型。",
        "score": 0.75,
        "title": "第二章_FastAPI 进阶",
        "metadata": {
            "document_id": "real_第二章_FastAPI_进阶_456",
            "heading_path_str": "第二章 > 2.2 自动校验",
        },
    },
]
search_knowledge.invoke({"query": "自动校验", "limit": 3})

# 模拟 _call_agent 第 2 次调用（合并已有 + 新 buffer）
buffered_round2 = pop_retrieval_buffer()
# 模拟 _call_agent 的合并逻辑
existing = retrieved_after_round1
retrieved_after_round2 = _merge_retrieved_docs(existing + buffered_round2)
print(f"  第 2 轮工具调用后: retrieved_docs={len(retrieved_after_round2)} 条")

# 生成最终 citations
final_citations = _build_citations(retrieved_after_round2)
print("\n  最终 citations:")
for c in final_citations:
    print(f"    [{c['index']}] score={c['score']:.2f} | {c['title']} | {c['heading_path']}")

assert len(final_citations) == 3, f"应有 3 条 citations（2+1），实际 {len(final_citations)}"
print(f"\n  ✓ 两轮工具调用合并: {len(final_citations)} 条 citations")

print("\n  ✓ 测试 5 通过")


# ============================================================
# 汇总
# ============================================================
print("\n" + "=" * 70)
print("✓ 引用溯源全部验证通过")
print("=" * 70)
print("""
引用溯源实现总结：

1. search_knowledge 工具（tools.py）
   - 返回带 [1][2] 编号的文本给 LLM，引导内联引用
   - 结构化结果写入模块级 buffer（线程安全）
   - 缓存命中时重放 artifact 到 buffer
   - 内容截断从 300 → 600 字符

2. pop_retrieval_buffer()（tools.py）
   - 读取并清空 buffer，供 Agent._call_agent 调用
   - LangGraph 单 session 内顺序执行，buffer 不会跨请求累积

3. _call_agent（agent.py）
   - 每次调用时 pop buffer + 合并已有 retrieved_docs
   - _merge_retrieved_docs 按 doc_id + content 去重
   - 支持多轮工具调用结果累积

4. _build_citations（agent.py）
   - 从 retrieved_docs 生成最终引用列表
   - 去重 + 按分数降序排序 + 重新编号
   - 精简字段（去掉 content），保留 image_path

5. API 层（langgraph.py）
   - /chat: 返回 citations 列表
   - /chat/stream: done 事件包含 citations
""")
