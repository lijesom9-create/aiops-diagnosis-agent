"""
RAG 检索层 QPS 压测脚本

测试场景：
1. 冷查询 QPS：N 个不同 query 并发，无缓存命中
2. 热查询 QPS：相同 query 并发，缓存命中
3. 混合 QPS：一半冷一半热

用法：
    cd backend
    $env:PYTHONPATH = "."; .\venv\Scripts\python.exe evaluation\perf\qps_test.py
"""

import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

from app.knowledge.unified_store import UnifiedKnowledgeStore
from app.retrieval.embeddings import create_embedding_model
from app.retrieval.reranker import CrossEncoderReranker

# 8 个不同 query（冷查询用）
COLD_QUERIES = [
    "什么是智能体？它有哪些基本要素？",
    "智能体的传统分类有哪些？",
    "如何构建一个 Agent 框架？",
    "FastAPI 是什么？它有什么优势？",
    "如何在 FastAPI 中定义路由和路径参数？",
    "什么是上下文工程？",
    "智能体通信协议有哪些？",
    "Agentic-RL 是什么？",
]


def fmt(n):
    if n < 1:
        return f"{n*1000:.0f}ms"
    return f"{n:.2f}s"


def run_concurrent(store, queries, concurrency, label):
    """并发执行检索，统计 QPS 和延迟分布"""
    print(f"\n{'='*60}")
    print(f"{label} | 并发数={concurrency} | 请求数={len(queries)}")
    print(f"{'='*60}")

    latencies = []
    errors = 0

    def _do_query(q):
        t0 = time.perf_counter()
        try:
            r = store.hybrid_search_parent_child(query=q, top_k=8, rewrite_mode="enhanced")
            return time.perf_counter() - t0, len(r), None
        except Exception as e:
            return time.perf_counter() - t0, 0, str(e)

    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_do_query, q): q for q in queries}
        for fut in as_completed(futures):
            latency, n_results, err = fut.result()
            if err:
                errors += 1
                print(f"  ERROR: {err[:60]}")
            else:
                latencies.append(latency)
    t_total = time.perf_counter() - t_start

    if not latencies:
        print("  无成功请求")
        return

    qps = len(latencies) / t_total
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p90 = latencies[int(len(latencies) * 0.9)]
    p99 = latencies[int(len(latencies) * 0.99)] if len(latencies) > 1 else latencies[-1]

    print(f"  成功: {len(latencies)} | 失败: {errors}")
    print(f"  总耗时: {fmt(t_total)}")
    print(f"  QPS: {qps:.2f}")
    print(f"  延迟: avg={fmt(statistics.mean(latencies))} p50={fmt(p50)} p90={fmt(p90)} p99={fmt(p99)}")
    return qps


def main():
    print("="*60)
    print("RAG 检索层 QPS 压测")
    print("="*60)

    print("\n初始化模型和知识库...")
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
    print(f"初始化完成: {fmt(time.perf_counter() - t0)}, 知识库: {store.size()} 条")

    # 预热：先跑一遍冷查询填缓存（用于热查询测试）
    print("\n预热缓存（跑一遍冷查询）...")
    for q in COLD_QUERIES:
        store.hybrid_search_parent_child(query=q, top_k=8, rewrite_mode="enhanced")

    # 清空缓存后再测冷查询
    from app.core.cache import get_cache
    cache = get_cache()
    cache.clear("unified_store_query")

    # 场景1：冷查询 QPS（不同 query，无缓存）
    # 8 个 query × 4 重复 = 32 个请求
    cold_queries = COLD_QUERIES * 4
    run_concurrent(store, cold_queries, concurrency=4, label="场景1: 冷查询（无缓存）")

    # 场景2：热查询 QPS（相同 query，缓存命中）
    # 用第一个 query 重复 32 次
    cache.clear("unified_store_query")
    # 先填一次缓存
    store.hybrid_search_parent_child(query=COLD_QUERIES[0], top_k=8, rewrite_mode="enhanced")
    hot_queries = [COLD_QUERIES[0]] * 32
    run_concurrent(store, hot_queries, concurrency=4, label="场景2: 热查询（缓存命中）")

    # 场景3：高并发冷查询（8 并发）
    cache.clear("unified_store_query")
    cold_queries_8 = COLD_QUERIES * 3  # 24 个
    run_concurrent(store, cold_queries_8, concurrency=8, label="场景3: 冷查询高并发（8并发）")

    # 场景4：热查询高并发
    cache.clear("unified_store_query")
    store.hybrid_search_parent_child(query=COLD_QUERIES[0], top_k=8, rewrite_mode="enhanced")
    hot_queries_8 = [COLD_QUERIES[0]] * 32
    run_concurrent(store, hot_queries_8, concurrency=8, label="场景4: 热查询高并发（8并发）")

    print(f"\n{'='*60}")
    print("压测完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
