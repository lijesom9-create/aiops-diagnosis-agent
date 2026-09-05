"""
扩展数据集 + 查询重写 + 重排器参数 综合消融实验

复用同一个 UnifiedKnowledgeStore，避免重复加载 embedding / cross-encoder 模型。
输出：markdown 表格 + JSON 结果文件
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

# 让脚本可以从 backend/ 目录运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evaluation.retrieval_eval import (
    RetrievalEvaluator,
    build_eval_index_from_files,
    build_expected_index,
    hybrid_rrf_search,
    load_file_queries,
    make_hybrid_rrf_cross_search,
    make_hybrid_rrf_pc_cross_search,
)
from app.retrieval.reranker import CrossEncoderReranker

DOCS_DIR = Path("evaluation/data/documents")
QUERIES_PATH = Path("evaluation/data/queries.json")
K = 5
RESULTS_JSON = Path("evaluation/results/ablation_1_3_4.json")


def evaluate_search_fn(
    search_fn: Callable[..., List[str]],
    store,
    queries: List[Dict[str, Any]],
    expected_index: Dict[str, List[str]],
    expected_parent_index: Dict[str, List[str]],
    use_parent_expected: bool,
    k: int = K,
) -> Dict[str, float]:
    """对单个搜索函数在所有查询上求平均指标"""
    evaluator = RetrievalEvaluator(k=k)
    expected = expected_parent_index if use_parent_expected else expected_index
    scores = []
    for case in queries:
        retrieved = search_fn(store, case["query"], case.get("source"), k)
        scores.append(
            evaluator.evaluate_case(retrieved, expected.get(case["query"], []))
        )
    return evaluator.aggregate(scores)


async def main():
    queries = load_file_queries(str(QUERIES_PATH))
    print(f"加载查询: {len(queries)} 条")

    store, _, persist_dir = await build_eval_index_from_files(
        docs_dir=str(DOCS_DIR),
        chunking_strategy="parent_child",
        parent_max_chars=800,
        child_max_chars=160,
        child_overlap_chars=30,
    )
    expected_index, expected_parent_index = build_expected_index(store, queries)
    print(f"构建期望索引: chunk={sum(len(v) for v in expected_index.values())}, "
          f"parent={sum(len(v) for v in expected_parent_index.values())}")

    # 加载单个 cross encoder，固定 max_length=512（已验证截断长度在该任务上影响很小）
    cross_reranker = CrossEncoderReranker(
        model_name="BAAI/bge-reranker-base", max_length=512
    )
    cross_reranker._load_model()

    records: List[Dict[str, Any]] = []

    def record(**kwargs):
        records.append(kwargs)
        return kwargs

    print("\n" + "=" * 110)
    print("1) 查询重写优化对比（hybrid_rrf / hybrid_rrf+cross, rrf_k=60）")
    print("=" * 110)
    print(f"{'rewrite_mode':<14}{'method':<22}{'recall@5':<12}{'mrr':<12}{'ndcg@5':<12}")
    print("-" * 110)

    for rewrite_mode in ("basic", "enhanced"):
        rrf_k = 60
        # 无重排 baseline
        def make_no_rr_search(rm=rewrite_mode, rk=rrf_k):
            def search(store, query, source, k, **kw):
                return hybrid_rrf_search(
                    store, query, source, k, rrf_k=rk,
                    rewrite_query=True, rewrite_mode=rm,
                    candidate_multiplier=3,
                )
            return search

        scores = evaluate_search_fn(
            make_no_rr_search(), store, queries, expected_index,
            expected_parent_index, use_parent_expected=False,
        )
        row = record(
            rewrite_mode=rewrite_mode, rrf_k=rrf_k,
            candidate_multiplier=3, cross_max_length=None,
            method="hybrid_rrf", **scores,
        )
        print(f"{row['rewrite_mode']:<14}{row['method']:<22}"
              f"{scores['recall@5']:<12.3f}{scores['mrr']:<12.3f}{scores['ndcg@5']:<12.3f}")

        # cross encoder 重排
        def make_cross_search(rm=rewrite_mode, rk=rrf_k):
            fn = make_hybrid_rrf_cross_search(cross_reranker)
            def search(store, query, source, k, **kw):
                return fn(
                    store, query, source, k, rrf_k=rk,
                    rewrite_query=True, rewrite_mode=rm,
                    candidate_multiplier=3,
                )
            return search

        scores = evaluate_search_fn(
            make_cross_search(), store, queries, expected_index,
            expected_parent_index, use_parent_expected=False,
        )
        row = record(
            rewrite_mode=rewrite_mode, rrf_k=rrf_k,
            candidate_multiplier=3, cross_max_length=512,
            method="hybrid_rrf+cross", **scores,
        )
        print(f"{row['rewrite_mode']:<14}{row['method']:<22}"
              f"{scores['recall@5']:<12.3f}{scores['mrr']:<12.3f}{scores['ndcg@5']:<12.3f}")

    print("\n" + "=" * 110)
    print("2) 重排器参数扫描（rewrite_mode=enhanced, rrf_k=60, cross_max_length=512）")
    print("=" * 110)
    print(f"{'multiplier':<12}{'method':<22}{'recall@5':<12}{'mrr':<12}{'ndcg@5':<12}")
    print("-" * 110)

    for candidate_multiplier in (2, 3, 4):
        fn = make_hybrid_rrf_cross_search(cross_reranker)

        def make_param_search(cm=candidate_multiplier, fn=fn):
            def search(store, query, source, k, **kw):
                return fn(
                    store, query, source, k, rrf_k=60,
                    rewrite_query=True, rewrite_mode="enhanced",
                    candidate_multiplier=cm,
                )
            return search

        scores = evaluate_search_fn(
            make_param_search(), store, queries, expected_index,
            expected_parent_index, use_parent_expected=False,
        )
        row = record(
            rewrite_mode="enhanced", rrf_k=60,
            candidate_multiplier=candidate_multiplier,
            cross_max_length=512,
            method="hybrid_rrf+cross", **scores,
        )
        print(f"{row['candidate_multiplier']:<12}{row['method']:<22}"
              f"{scores['recall@5']:<12.3f}{scores['mrr']:<12.3f}{scores['ndcg@5']:<12.3f}")

    print("\n" + "=" * 110)
    print("3) 父子文档检索增强重写验证（hybrid_rrf_pc+cross）")
    print("=" * 110)
    print(f"{'rewrite_mode':<14}{'method':<22}{'recall@5':<12}{'mrr':<12}{'ndcg@5':<12}")
    print("-" * 110)

    for rewrite_mode in ("basic", "enhanced"):
        fn = make_hybrid_rrf_pc_cross_search(cross_reranker)

        def make_pc_search(rm=rewrite_mode, fn=fn):
            def search(store, query, source, k, **kw):
                return fn(
                    store, query, source, k, rrf_k=60,
                    rewrite_query=True, rewrite_mode=rm,
                    candidate_multiplier=3,
                )
            return search

        scores = evaluate_search_fn(
            make_pc_search(), store, queries, expected_index,
            expected_parent_index, use_parent_expected=True,
        )
        row = record(
            rewrite_mode=rewrite_mode, rrf_k=60,
            candidate_multiplier=3, cross_max_length=512,
            method="hybrid_rrf_pc+cross", **scores,
        )
        print(f"{row['rewrite_mode']:<14}{row['method']:<22}"
              f"{scores['recall@5']:<12.3f}{scores['mrr']:<12.3f}{scores['ndcg@5']:<12.3f}")

    # 保存结果
    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_JSON.open("w", encoding="utf-8") as f:
        json.dump(
            {"queries": len(queries), "persist_dir": persist_dir, "results": records},
            f, ensure_ascii=False, indent=2,
        )
    print(f"\n结果已保存: {RESULTS_JSON}")


if __name__ == "__main__":
    asyncio.run(main())
