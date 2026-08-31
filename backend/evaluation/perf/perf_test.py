"""
RAG 性能优化实测脚本

测试场景：
1. Embedding 缓存：相同文本多次嵌入，对比首次 vs 后续
2. Reranker 缓存：相同 query-doc 对多次打分，对比首次 vs 后续
3. 检索结果缓存：相同 query 多次检索，对比首次 vs 后续
4. 并行检索：多个不同 query 的检索耗时
5. 汇总报告

用法：
    cd backend
    python evaluation/perf_test.py
"""

import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# 降低日志级别，避免输出干扰测试结果
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from app.knowledge.unified_store import UnifiedKnowledgeStore
from app.retrieval.embeddings import create_embedding_model
from app.retrieval.reranker import CrossEncoderReranker

# ========== 测试用例 ==========

TEST_TEXTS = [
    "什么是智能体？它有哪些基本要素？",
    "如何在 FastAPI 中定义路由和路径参数？",
    "什么是上下文工程？它在智能体中起什么作用？",
    "智能体通信协议有哪些？它们如何工作？",
]

TEST_QUERIES = [
    "什么是智能体？它有哪些基本要素？",
    "智能体的传统分类有哪些？",
    "如何构建一个 Agent 框架？",
    "FastAPI 是什么？它有什么优势？",
    "如何在 FastAPI 中定义路由和路径参数？",
    "什么是上下文工程？",
    "智能体通信协议有哪些？",
    "Agentic-RL 是什么？",
]


def fmt_ms(ms: float) -> str:
    """格式化毫秒输出"""
    if ms < 1:
        return f"{ms*1000:.0f}μs"
    if ms < 1000:
        return f"{ms:.1f}ms"
    return f"{ms/1000:.2f}s"


def test_embedding_cache(embedding_model):
    """测试 1：Embedding 缓存"""
    print("\n" + "=" * 70)
    print("测试 1：Embedding 缓存效果")
    print("=" * 70)
    print(f"测试文本数: {len(TEST_TEXTS)}，每条重复嵌入 5 次\n")

    all_first = []
    all_cached = []

    for text in TEST_TEXTS:
        # 首次嵌入（未命中缓存）
        start = time.perf_counter()
        vec1 = embedding_model.embed(text)
        t_first = (time.perf_counter() - start) * 1000
        all_first.append(t_first)

        # 后续 4 次（应命中缓存）
        cached_times = []
        for _ in range(4):
            start = time.perf_counter()
            vec2 = embedding_model.embed(text)
            t = (time.perf_counter() - start) * 1000
            cached_times.append(t)
            all_cached.append(t)

        consistent = vec1 == vec2
        avg_cached = statistics.mean(cached_times)
        speedup = t_first / max(avg_cached, 0.001)

        print(f"  文本: '{text[:25]}...'")
        print(f"    首次: {fmt_ms(t_first)} | 缓存平均: {fmt_ms(avg_cached)} | 加速: {speedup:.1f}x | 一致: {consistent}")
        print(f"    后续 4 次: {[f'{t:.2f}ms' for t in cached_times]}")
        print()

    stats = embedding_model.cache_stats() if hasattr(embedding_model, "cache_stats") else {}
    print(f"  [汇总] 首次平均: {fmt_ms(statistics.mean(all_first))} | 缓存平均: {fmt_ms(statistics.mean(all_cached))}")
    print(f"  [缓存统计] {stats}")
    return statistics.mean(all_first), statistics.mean(all_cached)


def test_reranker_cache(reranker):
    """测试 2：Reranker 缓存"""
    print("\n" + "=" * 70)
    print("测试 2：Reranker 缓存效果")
    print("=" * 70)

    from app.retrieval.base import RetrievalResult
    query = "什么是智能体"
    results = [
        RetrievalResult(doc_id="1", content="智能体是具备感知、决策、行动能力的实体", score=0.5, metadata={}, source="test"),
        RetrievalResult(doc_id="2", content="FastAPI 是一个 Web 框架", score=0.4, metadata={}, source="test"),
        RetrievalResult(doc_id="3", content="上下文工程是关于提示词设计的方法论", score=0.3, metadata={}, source="test"),
    ]

    # 首次重排
    start = time.perf_counter()
    reranker.rerank(query, results.copy(), limit=3)
    t_first = (time.perf_counter() - start) * 1000

    # 后续 4 次（相同 query-doc 对，应命中缓存）
    cached_times = []
    for _ in range(4):
        start = time.perf_counter()
        reranker.rerank(query, results.copy(), limit=3)
        t = (time.perf_counter() - start) * 1000
        cached_times.append(t)

    avg_cached = statistics.mean(cached_times)
    speedup = t_first / max(avg_cached, 0.001)

    print(f"  query: '{query}'")
    print(f"  候选文档数: {len(results)}")
    print(f"  首次 rerank: {fmt_ms(t_first)}")
    print(f"  缓存命中:    {fmt_ms(avg_cached)} (4 次平均)")
    print(f"  后续 4 次:   {[f'{t:.2f}ms' for t in cached_times]}")
    print(f"  加速比:      {speedup:.1f}x")
    print(f"  缓存命中数:  {reranker._cache_hits}")
    print(f"  缓存未命中:  {reranker._cache_misses}")
    return t_first, avg_cached


def test_search_cache(store):
    """测试 3：检索结果缓存"""
    print("\n" + "=" * 70)
    print("测试 3：检索结果缓存效果（hybrid_search_parent_child）")
    print("=" * 70)
    print(f"测试 query 数: {len(TEST_QUERIES)}，每个 query 检索 2 次\n")

    all_first = []
    all_cached = []

    for q in TEST_QUERIES:
        # 首次检索
        start = time.perf_counter()
        r1 = store.hybrid_search_parent_child(query=q, top_k=8, rewrite_mode="enhanced")
        t_first = (time.perf_counter() - start) * 1000
        all_first.append(t_first)

        # 第二次（应命中缓存）
        start = time.perf_counter()
        r2 = store.hybrid_search_parent_child(query=q, top_k=8, rewrite_mode="enhanced")
        t_cached = (time.perf_counter() - start) * 1000
        all_cached.append(t_cached)

        speedup = t_first / max(t_cached, 0.001)
        consistent = len(r1) == len(r2)
        print(f"  q: '{q[:30]}...'")
        print(f"    首次: {fmt_ms(t_first)} | 缓存: {fmt_ms(t_cached)} | 加速: {speedup:.1f}x | 结果一致: {consistent} (n={len(r1)})")

    print(f"\n  [汇总] 首次平均: {fmt_ms(statistics.mean(all_first))} | 缓存平均: {fmt_ms(statistics.mean(all_cached))}")
    avg_speedup = statistics.mean(all_first) / max(statistics.mean(all_cached), 0.001)
    print(f"  [汇总] 平均加速比: {avg_speedup:.1f}x")
    return statistics.mean(all_first), statistics.mean(all_cached)


def test_parallel_search(store):
    """测试 4：并行检索（不同 query）"""
    print("\n" + "=" * 70)
    print("测试 4：并行检索效果（hybrid_search，不同 query）")
    print("=" * 70)

    times = []
    for q in TEST_QUERIES:
        start = time.perf_counter()
        results = store.hybrid_search(query=q, top_k=5, rewrite_mode="enhanced")
        t = (time.perf_counter() - start) * 1000
        times.append(t)
        print(f"  q: '{q[:30]}...' -> {fmt_ms(t)}, 结果数={len(results)}")

    print(f"\n  [汇总] 平均: {fmt_ms(statistics.mean(times))} | 最小: {fmt_ms(min(times))} | 最大: {fmt_ms(max(times))}")


def main():
    print("=" * 70)
    print("RAG 性能优化实测报告")
    print("=" * 70)

    # 初始化
    print("\n正在初始化模型和知识库...")
    t0 = time.perf_counter()
    embedding_model = create_embedding_model(local_model_name="BAAI/bge-small-zh-v1.5")
    reranker = CrossEncoderReranker(model_name="BAAI/bge-reranker-base")
    reranker._load_model()
    store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        reranker=reranker,
        separate_parent_child=True,
        vector_store_backend="qdrant",
    )
    print(f"初始化完成，耗时 {fmt_ms((time.perf_counter() - t0) * 1000)}")
    print(f"知识库记录数: {store.size()}")

    # 执行测试
    emb_first, emb_cached = test_embedding_cache(embedding_model)
    rer_first, rer_cached = test_reranker_cache(reranker)
    search_first, search_cached = test_search_cache(store)
    test_parallel_search(store)

    # 汇总
    print("\n" + "=" * 70)
    print("性能优化汇总报告")
    print("=" * 70)
    print(f"{'优化项':<25} {'首次耗时':<12} {'缓存命中':<12} {'加速比':<10}")
    print("-" * 70)
    print(f"{'Embedding 缓存':<25} {fmt_ms(emb_first):<12} {fmt_ms(emb_cached):<12} {emb_first/max(emb_cached,0.001):.1f}x")
    print(f"{'Reranker 缓存':<25} {fmt_ms(rer_first):<12} {fmt_ms(rer_cached):<12} {rer_first/max(rer_cached,0.001):.1f}x")
    print(f"{'检索结果缓存':<25} {fmt_ms(search_first):<12} {fmt_ms(search_cached):<12} {search_first/max(search_cached,0.001):.1f}x")
    print("=" * 70)

    # 刷盘
    if hasattr(embedding_model, "flush"):
        embedding_model.flush()
        print("\nEmbedding 磁盘缓存已刷盘")


if __name__ == "__main__":
    main()
