"""
RAG 参数综合扫描实验

一次性跑完 candidate_multiplier / rrf_k / vector_weight / top_k 对比
共用 CrossEncoder 实例，避免重复加载模型

用法：
    python -u -m app.evaluation.param_sweep --docs-dir evaluation/data/documents --queries evaluation/data/queries.json
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Dict, Set

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 离线模式，避免 HF 连接超时
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("VECTOR_STORE_BACKEND", "chroma")


def _build_store(embedding_model, persist_dir):
    """构建带父子分离 + CrossEncoder reranker 的 store"""
    from app.knowledge.unified_store import UnifiedKnowledgeStore
    from app.retrieval.reranker import CrossEncoderReranker

    reranker = CrossEncoderReranker(
        model_name="BAAI/bge-reranker-base",
        max_length=512,
    )
    reranker._load_model()

    store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        collection_name="param_sweep",
        persist_directory=persist_dir,
        reranker=reranker,
        vector_store_backend="chroma",
        separate_parent_child=True,
    )
    return store


def _index_documents(store, docs_dir):
    """索引文档（同步包装 async upload）"""
    import asyncio
    import glob

    from app.document.uploader import DocumentUploader

    uploader = DocumentUploader(
        knowledge_store=store,
        chunking_strategy="parent_child",
    )

    async def _upload_all():
        files = sorted(glob.glob(os.path.join(docs_dir, "*.md")))
        for f in files:
            with open(f, "rb") as fh:
                content = fh.read()
            await uploader.upload(
                content=content,
                filename=os.path.basename(f),
                title=os.path.splitext(os.path.basename(f))[0],
            )
            print(f"    indexed: {os.path.basename(f)}")
        return len(files)

    count = asyncio.run(_upload_all())
    return count


def _load_queries(queries_path):
    """加载查询集"""
    with open(queries_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["queries"] if "queries" in data else data


def _build_expected_mapping(store, queries):
    """构建 query -> expected_parent_ids 的映射

    逻辑：扫描 child_store 的所有 chunks，找出包含任意 expected_keyword 的 chunk，
    取其 parent_id 作为 expected_parent_ids
    """
    all_child = store.vector_store.get_all(include=["documents", "metadatas"])
    child_ids = all_child.get("ids", [])
    child_docs = all_child.get("documents", [])
    child_metas = all_child.get("metadatas", []) or [{}] * len(child_ids)

    query_to_expected: Dict[str, Set[str]] = {}
    for q in queries:
        keywords = q.get("expected_keywords", [])
        if not keywords:
            query_to_expected[q["query"]] = set()
            continue

        expected_parent_ids: Set[str] = set()
        for i, doc_text in enumerate(child_docs):
            if doc_text and any(kw in doc_text for kw in keywords):
                parent_id = child_metas[i].get("parent_id") if i < len(child_metas) else None
                if parent_id:
                    expected_parent_ids.add(parent_id)

        # 如果没找到 parent_id，用 child_id 本身
        if not expected_parent_ids:
            for i, doc_text in enumerate(child_docs):
                if doc_text and any(kw in doc_text for kw in keywords):
                    expected_parent_ids.add(child_ids[i])

        query_to_expected[q["query"]] = expected_parent_ids

    return query_to_expected


def _recall_at_k(retrieved_ids, expected_ids, k):
    if not expected_ids:
        return 0.0
    hits = sum(1 for rid in retrieved_ids[:k] if rid in expected_ids)
    return hits / len(expected_ids)


def _mrr(retrieved_ids, expected_ids):
    for i, rid in enumerate(retrieved_ids):
        if rid in expected_ids:
            return 1.0 / (i + 1)
    return 0.0


def _ndcg_at_k(retrieved_ids, expected_ids, k):
    import math
    expected = set(expected_ids)
    dcg = sum(
        (1.0 if rid in expected else 0.0) / math.log2(i + 2)
        for i, rid in enumerate(retrieved_ids[:k])
    )
    ideal = min(len(expected_ids), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def _evaluate(store, queries, expected_mapping, top_k=5, **search_kwargs):
    """跑评测，返回 recall/mrr/ndcg"""
    results = []
    for q in queries:
        query_text = q["query"]
        expected_ids = expected_mapping.get(query_text, set())
        if not expected_ids:
            continue

        search_results = store.hybrid_search_parent_child(
            query=query_text,
            top_k=top_k,
            rewrite_query=True,
            rewrite_mode="enhanced",
            **search_kwargs,
        )

        retrieved_ids = [r["id"] for r in search_results]

        results.append({
            "recall": _recall_at_k(retrieved_ids, expected_ids, top_k),
            "mrr": _mrr(retrieved_ids, expected_ids),
            "ndcg": _ndcg_at_k(retrieved_ids, expected_ids, top_k),
        })

    n = len(results)
    if n == 0:
        return {"recall@5": 0, "mrr": 0, "ndcg@5": 0, "n": 0}

    return {
        "recall@5": sum(r["recall"] for r in results) / n,
        "mrr": sum(r["mrr"] for r in results) / n,
        "ndcg@5": sum(r["ndcg"] for r in results) / n,
        "n": n,
    }


def _print_table(title, results):
    """打印对比表"""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    print(f"{'config':<30} {'recall@5':<12} {'mrr':<12} {'ndcg@5':<12} {'n':<5}")
    print(f"{'-'*80}")
    for config, metrics in results:
        print(
            f"{config:<30} "
            f"{metrics['recall@5']:<12.3f} "
            f"{metrics['mrr']:<12.3f} "
            f"{metrics['ndcg@5']:<12.3f} "
            f"{metrics['n']:<5}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(description="RAG 参数综合扫描")
    parser.add_argument("--docs-dir", default="evaluation/data/documents")
    parser.add_argument("--queries", default="evaluation/data/queries.json")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", default="evaluation/results/param_sweep_results.json")
    args = parser.parse_args()

    print("=" * 80)
    print("  RAG 参数综合扫描实验")
    print("=" * 80)
    print(f"  文档目录: {args.docs_dir}")
    print(f"  查询文件: {args.queries}")
    print(f"  top_k: {args.top_k}")

    # 加载 embedding 模型
    print("\n[1/4] 加载 embedding 模型...")
    from app.retrieval.embeddings import create_embedding_model
    embedding_model = create_embedding_model()
    print("  done")

    # 构建索引（共用）
    print("\n[2/4] 构建索引（父子分离存储）...")
    persist_dir = tempfile.mkdtemp(prefix="param_sweep_")
    t0 = time.time()
    store = _build_store(embedding_model, persist_dir)
    _index_documents(store, args.docs_dir)
    build_time = time.time() - t0
    print(f"  done, {build_time:.1f}s, child={store.vector_store.size()}, parent={store._parent_store.size()}")

    # 加载查询 + 构建 expected mapping
    print("\n[3/4] 加载查询 + 构建 expected mapping...")
    queries = _load_queries(args.queries)
    expected_mapping = _build_expected_mapping(store, queries)
    print(f"  done, {len(queries)} queries, {sum(1 for v in expected_mapping.values() if v)} have expected_ids")

    # ========== 参数扫描 ==========
    print("\n[4/4] 开始参数扫描...")
    all_results = {}

    # 实验 A: candidate_multiplier
    print("\n--- 实验 A: candidate_multiplier 扫描 ---")
    exp_a = []
    for mult in [2, 3, 4, 5]:
        t0 = time.time()
        metrics = _evaluate(store, queries, expected_mapping, top_k=args.top_k, candidate_multiplier=mult)
        elapsed = time.time() - t0
        config = f"multiplier={mult}"
        exp_a.append((config, metrics))
        print(f"  {config}: recall={metrics['recall@5']:.3f}, mrr={metrics['mrr']:.3f}, ndcg={metrics['ndcg@5']:.3f} ({elapsed:.1f}s)")
    _print_table("实验 A: candidate_multiplier 扫描", exp_a)
    all_results["exp_a_candidate_multiplier"] = exp_a

    # 实验 B: rrf_k
    print("\n--- 实验 B: rrf_k 扫描 ---")
    exp_b = []
    for rrf_k in [10, 30, 60, 100, 200]:
        t0 = time.time()
        metrics = _evaluate(store, queries, expected_mapping, top_k=args.top_k, rrf_k=rrf_k)
        elapsed = time.time() - t0
        config = f"rrf_k={rrf_k}"
        exp_b.append((config, metrics))
        print(f"  {config}: recall={metrics['recall@5']:.3f}, mrr={metrics['mrr']:.3f}, ndcg={metrics['ndcg@5']:.3f} ({elapsed:.1f}s)")
    _print_table("实验 B: rrf_k 扫描", exp_b)
    all_results["exp_b_rrf_k"] = exp_b

    # 实验 C: RRF 权重
    print("\n--- 实验 C: RRF 权重扫描 (vector_weight : bm25_weight) ---")
    exp_c = []
    weight_configs = [
        ("v1.0:b1.0(默认)", 1.0, 1.0),
        ("v0.7:b0.3", 0.7, 0.3),
        ("v0.3:b0.7", 0.3, 0.7),
        ("v1.5:b1.0", 1.5, 1.0),
        ("v1.0:b1.5", 1.0, 1.5),
        ("v2.0:b1.0", 2.0, 1.0),
        ("v1.0:b2.0", 1.0, 2.0),
        ("v3.0:b1.0", 3.0, 1.0),
        ("v1.0:b3.0", 1.0, 3.0),
    ]
    for name, vw, bw in weight_configs:
        t0 = time.time()
        metrics = _evaluate(store, queries, expected_mapping, top_k=args.top_k, vector_weight=vw, bm25_weight=bw)
        elapsed = time.time() - t0
        config = f"weight={name}"
        exp_c.append((config, metrics))
        print(f"  {config}: recall={metrics['recall@5']:.3f}, mrr={metrics['mrr']:.3f}, ndcg={metrics['ndcg@5']:.3f} ({elapsed:.1f}s)")
    _print_table("实验 C: RRF 权重扫描", exp_c)
    all_results["exp_c_rrf_weight"] = exp_c

    # 实验 D: top_k
    print("\n--- 实验 D: top_k 扫描 ---")
    exp_d = []
    for tk in [3, 5, 8, 10]:
        t0 = time.time()
        metrics = _evaluate(store, queries, expected_mapping, top_k=tk)
        elapsed = time.time() - t0
        config = f"top_k={tk}"
        exp_d.append((config, metrics))
        print(f"  {config}: recall={metrics['recall@5']:.3f}, mrr={metrics['mrr']:.3f}, ndcg={metrics['ndcg@5']:.3f} ({elapsed:.1f}s)")
    _print_table("实验 D: top_k 扫描", exp_d)
    all_results["exp_d_top_k"] = exp_d

    # ========== 汇总 ==========
    print("\n" + "=" * 80)
    print("  汇总：各实验最优配置")
    print("=" * 80)

    for exp_name, results in all_results.items():
        best = max(results, key=lambda x: x[1]["ndcg@5"])
        print(f"  {exp_name}:")
        print(f"    最优 = {best[0]} (ndcg={best[1]['ndcg@5']:.3f}, recall={best[1]['recall@5']:.3f}, mrr={best[1]['mrr']:.3f})")

    # 保存结果
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    serializable = {
        k: [{"config": c, **m} for c, m in v]
        for k, v in all_results.items()
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)
    print(f"\n结果已保存到: {args.output}")

    # 清理
    try:
        shutil.rmtree(persist_dir, ignore_errors=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
