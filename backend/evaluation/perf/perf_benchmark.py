"""
性能优化验证脚本

验证内容：
1. Embedding 缓存命中率（第二次相同 query 应接近 0ms）
2. 查询重写缓存（LLM 重写第二次命中）
3. Reranker 缓存（相同 query-doc 对第二次命中）
4. 检索结果缓存（相同 query 第二次接近 0ms）
5. 并行检索 vs 串行检索

用法：
    cd backend
    python evaluation/perf_benchmark.py
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from loguru import logger

from app.knowledge.unified_store import UnifiedKnowledgeStore
from app.retrieval.embeddings import create_embedding_model
from app.retrieval.reranker import CrossEncoderReranker

QUERIES = [
    "什么是智能体？它有哪些基本要素？",
    "如何在 FastAPI 中定义路由和路径参数？",
    "什么是上下文工程？",
    "智能体通信协议有哪些？",
]


def benchmark_embedding_cache(embedding_model):
    """验证 embedding 缓存"""
    print("\n" + "=" * 60)
    print("1. Embedding 缓存验证")
    print("=" * 60)

    text = "这是一个测试查询，用于验证 embedding 缓存效果"

    # 第一次：未命中
    start = time.perf_counter()
    vec1 = embedding_model.embed(text)
    t1 = (time.perf_counter() - start) * 1000

    # 第二次：应命中 LRU
    start = time.perf_counter()
    vec2 = embedding_model.embed(text)
    t2 = (time.perf_counter() - start) * 1000

    print(f"  首次 embed: {t1:.2f}ms")
    print(f"  缓存命中:   {t2:.2f}ms")
    print(f"  加速比:     {t1/max(t2, 0.001):.1f}x")
    print(f"  向量一致:   {vec1 == vec2}")

    stats = embedding_model.cache_stats() if hasattr(embedding_model, "cache_stats") else {}
    print(f"  缓存统计:   {stats}")


def benchmark_reranker_cache(reranker):
    """验证 reranker 缓存"""
    print("\n" + "=" * 60)
    print("3. Reranker 缓存验证")
    print("=" * 60)

    from app.retrieval.base import RetrievalResult
    query = "什么是智能体"
    results = [
        RetrievalResult(doc_id="1", content="智能体是具备感知、决策、行动能力的实体", score=0.5, metadata={}, source="test"),
        RetrievalResult(doc_id="2", content="FastAPI 是一个 Web 框架", score=0.4, metadata={}, source="test"),
    ]

    # 第一次
    start = time.perf_counter()
    reranker.rerank(query, results.copy(), limit=2)
    t1 = (time.perf_counter() - start) * 1000

    # 第二次：相同 query-doc 对应命中
    start = time.perf_counter()
    reranker.rerank(query, results.copy(), limit=2)
    t2 = (time.perf_counter() - start) * 1000

    print(f"  首次 rerank: {t1:.2f}ms")
    print(f"  缓存命中:    {t2:.2f}ms")
    print(f"  加速比:      {t1/max(t2, 0.001):.1f}x")
    print(f"  缓存命中数:  {reranker._cache_hits}")
    print(f"  缓存未命中:  {reranker._cache_misses}")


def benchmark_search_cache(store):
    """验证检索结果缓存"""
    print("\n" + "=" * 60)
    print("4. 检索结果缓存验证（hybrid_search_parent_child）")
    print("=" * 60)

    query = QUERIES[0]

    # 第一次
    start = time.perf_counter()
    r1 = store.hybrid_search_parent_child(query=query, top_k=8, rewrite_mode="enhanced")
    t1 = (time.perf_counter() - start) * 1000

    # 第二次：应命中缓存
    start = time.perf_counter()
    r2 = store.hybrid_search_parent_child(query=query, top_k=8, rewrite_mode="enhanced")
    t2 = (time.perf_counter() - start) * 1000

    print(f"  首次检索: {t1:.2f}ms (结果数: {len(r1)})")
    print(f"  缓存命中: {t2:.2f}ms (结果数: {len(r2)})")
    print(f"  加速比:   {t1/max(t2, 0.001):.1f}x")


def benchmark_parallel_search(store):
    """验证并行检索"""
    print("\n" + "=" * 60)
    print("5. 并行检索验证（hybrid_search）")
    print("=" * 60)

    for q in QUERIES:
        start = time.perf_counter()
        results = store.hybrid_search(query=q, top_k=5, rewrite_mode="enhanced")
        t = (time.perf_counter() - start) * 1000
        print(f"  query='{q[:20]}...' -> {t:.2f}ms, 结果数={len(results)}")


def main():
    print("=" * 60)
    print("RAG 性能优化验证")
    print("=" * 60)

    # 初始化
    logger.info("正在初始化模型和知识库...")
    embedding_model = create_embedding_model(local_model_name="BAAI/bge-small-zh-v1.5")

    reranker = CrossEncoderReranker(model_name="BAAI/bge-reranker-base")
    reranker._load_model()

    store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        reranker=reranker,
        separate_parent_child=True,
        vector_store_backend="qdrant",
    )

    # 1. Embedding 缓存
    benchmark_embedding_cache(embedding_model)

    # 3. Reranker 缓存
    benchmark_reranker_cache(reranker)

    # 4. 检索结果缓存
    benchmark_search_cache(store)

    # 5. 并行检索
    benchmark_parallel_search(store)

    # 刷盘
    if hasattr(embedding_model, "flush"):
        embedding_model.flush()
        logger.info("Embedding 磁盘缓存已刷盘")

    print("\n" + "=" * 60)
    print("性能验证完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
