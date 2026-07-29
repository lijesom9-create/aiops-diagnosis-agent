"""
检索效果评测工具

用于评估 RAG 检索链路在不同策略下的效果，支持：
- 向量检索
- BM25 检索
- RRF 混合检索
- RRF + SimpleReranker
- RRF + CrossEncoderReranker

输出指标：Recall@K、MRR、NDCG@K
"""

import argparse
import asyncio
import json
import math
import os
import shutil
import statistics
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

from app.document.uploader import DocumentUploader
from app.knowledge.unified_store import KnowledgeItem, UnifiedKnowledgeStore
from app.retrieval.embeddings import TFIDFModel, create_embedding_model
from app.retrieval.reranker import CrossEncoderReranker


EvalCase = Dict[str, Any]
SearchFn = Callable[..., List[str]]


def recall_at_k(retrieved_ids: List[str], expected_ids: List[str], k: int) -> float:
    """Recall@K：top-k 中命中相关文档的比例"""
    if not expected_ids:
        return 0.0
    retrieved_k = set(retrieved_ids[:k])
    return len(retrieved_k & set(expected_ids)) / len(expected_ids)


def mrr(retrieved_ids: List[str], expected_ids: List[str]) -> float:
    """MRR：第一个相关文档排名的倒数"""
    expected = set(expected_ids)
    for rank, doc_id in enumerate(retrieved_ids, start=1):
        if doc_id in expected:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved_ids: List[str], expected_ids: List[str], k: int) -> float:
    """NDCG@K：考虑相关文档位置的折损累计增益"""
    expected = set(expected_ids)
    dcg = 0.0
    for i, doc_id in enumerate(retrieved_ids[:k]):
        rel = 1.0 if doc_id in expected else 0.0
        dcg += (2**rel - 1) / math.log2(i + 2)

    ideal = min(len(expected_ids), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal))
    return dcg / idcg if idcg > 0 else 0.0


class RetrievalEvaluator:
    """检索效果评估器"""

    def __init__(self, k: int = 5):
        self.k = k

    def evaluate_case(
        self,
        retrieved_ids: List[str],
        expected_ids: List[str],
    ) -> Dict[str, float]:
        return {
            f"recall@{self.k}": recall_at_k(retrieved_ids, expected_ids, self.k),
            "mrr": mrr(retrieved_ids, expected_ids),
            f"ndcg@{self.k}": ndcg_at_k(retrieved_ids, expected_ids, self.k),
        }

    def aggregate(self, scores: List[Dict[str, float]]) -> Dict[str, float]:
        return {
            metric: statistics.mean([s[metric] for s in scores])
            for metric in scores[0].keys()
        }


# ========== 检索策略 ==========


def vector_only_search(
    store: UnifiedKnowledgeStore,
    query: str,
    source: Optional[str],
    top_k: int,
    rrf_k: int = 60,
    **kwargs,
) -> List[str]:
    """纯向量检索"""
    filters = {"source": source} if source else None
    results = store.vector_store.search(
        query=query,
        top_k=top_k,
        min_score=0.0,
        filters=filters,
    )
    return [doc_id for doc_id, _, _ in results]


def bm25_only_search(
    store: UnifiedKnowledgeStore,
    query: str,
    source: Optional[str],
    top_k: int,
    rrf_k: int = 60,
    **kwargs,
) -> List[str]:
    """纯 BM25 检索（按 source 后过滤）"""
    store._ensure_bm25_index()
    candidates = store._bm25.search(query, top_k=top_k * 3)

    ids = []
    for doc_id, _ in candidates:
        if len(ids) >= top_k:
            break
        record = store.vector_store.get_by_ids([doc_id])
        if not record:
            continue
        meta = record[0].get("metadata", {})
        if source and meta.get("source") != source:
            continue
        ids.append(doc_id)
    return ids


def hybrid_rrf_search(
    store: UnifiedKnowledgeStore,
    query: str,
    source: Optional[str],
    top_k: int,
    rrf_k: int = 60,
    rewrite_query: bool = True,
    rewrite_mode: str = "basic",
    candidate_multiplier: int = 3,
    **kwargs,
) -> List[str]:
    """RRF 混合检索，不重排"""
    results = store.hybrid_search(
        query=query,
        top_k=top_k,
        source=source,
        rewrite_query=rewrite_query,
        rewrite_mode=rewrite_mode,
        min_score=0.0,
        rrf_k=rrf_k,
        candidate_multiplier=candidate_multiplier,
    )
    return [r["id"] for r in results]


def hybrid_rrf_pc_search(
    store: UnifiedKnowledgeStore,
    query: str,
    source: Optional[str],
    top_k: int,
    rrf_k: int = 60,
    rewrite_query: bool = True,
    rewrite_mode: str = "basic",
    candidate_multiplier: int = 3,
    **kwargs,
) -> List[str]:
    """父子文档 RRF 混合检索，不重排"""
    results = store.hybrid_search_parent_child(
        query=query,
        top_k=top_k,
        source=source,
        rewrite_query=rewrite_query,
        rewrite_mode=rewrite_mode,
        min_score=0.0,
        rrf_k=rrf_k,
        candidate_multiplier=candidate_multiplier,
    )
    return [r["id"] for r in results]


def _hybrid_with_reranker(
    store: UnifiedKnowledgeStore,
    query: str,
    source: Optional[str],
    top_k: int,
    rrf_k: int,
    rewrite_query: bool,
    rewrite_mode: str,
    candidate_multiplier: int,
    reranker,
    use_parent_child: bool = False,
) -> List[str]:
    """临时挂载 reranker 执行混合检索"""
    original = store.reranker
    store.reranker = reranker
    try:
        search_fn = (
            store.hybrid_search_parent_child
            if use_parent_child
            else store.hybrid_search
        )
        results = search_fn(
            query=query,
            top_k=top_k,
            source=source,
            rewrite_query=rewrite_query,
            rewrite_mode=rewrite_mode,
            min_score=0.0,
            rrf_k=rrf_k,
            candidate_multiplier=candidate_multiplier,
        )
        return [r["id"] for r in results]
    finally:
        store.reranker = original


def make_hybrid_rrf_cross_search(reranker: CrossEncoderReranker):
    """返回绑定 CrossEncoderReranker 的搜索函数（避免重复加载模型）"""
    def search(
        store: UnifiedKnowledgeStore,
        query: str,
        source: Optional[str],
        top_k: int,
        rrf_k: int = 60,
        rewrite_query: bool = True,
        rewrite_mode: str = "basic",
        candidate_multiplier: int = 3,
        **kwargs,
    ) -> List[str]:
        return _hybrid_with_reranker(
            store, query, source, top_k, rrf_k,
            rewrite_query, rewrite_mode, candidate_multiplier, reranker,
        )
    return search


def make_hybrid_rrf_pc_cross_search(reranker: CrossEncoderReranker):
    """返回绑定 CrossEncoderReranker 的父子文档搜索函数"""
    def search(
        store: UnifiedKnowledgeStore,
        query: str,
        source: Optional[str],
        top_k: int,
        rrf_k: int = 60,
        rewrite_query: bool = True,
        rewrite_mode: str = "basic",
        candidate_multiplier: int = 3,
        **kwargs,
    ) -> List[str]:
        return _hybrid_with_reranker(
            store, query, source, top_k, rrf_k,
            rewrite_query, rewrite_mode, candidate_multiplier, reranker,
            use_parent_child=True,
        )
    return search


# ========== 评测数据集 ==========


COURSE_CORPUS = [
    {
        "id": "course_decorator_001",
        "title": "Python 装饰器",
        "content": (
            "装饰器是 Python 中用于修改函数或方法行为的高级特性。"
            "它可以在不修改原函数源代码的情况下，为函数添加额外功能。"
            "装饰器本质上是一个接收函数并返回函数的高阶函数。"
        ),
    },
    {
        "id": "course_recursion_001",
        "title": "递归",
        "content": (
            "递归是函数在定义中直接或间接调用自身的编程技巧。"
            "一个正确的递归算法必须包含基准情形和递归情形两部分。"
            "基准情形用于终止递归，递归情形把问题规模缩小。"
        ),
    },
    {
        "id": "course_dp_001",
        "title": "动态规划",
        "content": (
            "动态规划是一种通过把原问题分解为相对简单的子问题来求解复杂问题的方法。"
            "它适用于具有重叠子问题和最优子结构性质的问题。"
            "常见的动态规划例子包括斐波那契数列、最长公共子序列和背包问题。"
        ),
    },
]

USER_DOC_ALGORITHMS = """# 算法基础

## 1.1 递归
递归是函数调用自身的过程。递归包含两个部分：基准情形和递归情形。

## 1.2 分治
分治策略将问题分解为更小的子问题，然后分别解决并合并结果。

## 1.3 动态规划
动态规划通过存储子问题的解来避免重复计算，通常使用记忆化或填表实现。
"""

USER_DOC_PYTHON = """# Python 技巧

## 列表推导式
列表推导式提供了一种简洁的方式创建列表，例如 `[x*2 for x in range(10)]`。

## 生成器
生成器使用 `yield` 关键字返回一个迭代器，可以惰性生成大量数据而不会一次性占用大量内存。
"""

EVAL_QUERIES: List[EvalCase] = [
    {
        "query": "什么是装饰器",
        "source": "course",
        "expected_keywords": ["装饰器"],
        "description": "课程知识关键词匹配",
    },
    {
        "query": "递归的定义",
        "source": "course",
        "expected_keywords": ["递归"],
        "description": "课程知识语义匹配",
    },
    {
        "query": "动态规划适用场景",
        "source": None,
        "expected_keywords": ["动态规划"],
        "description": "跨来源召回",
    },
    {
        "query": "递归的终止条件",
        "source": "user_document",
        "expected_keywords": ["递归", "基准情形"],
        "description": "用户文档 parent-child",
    },
    {
        "query": "分治策略是什么",
        "source": "user_document",
        "expected_keywords": ["分治"],
        "description": "用户文档语义匹配",
    },
    {
        "query": "生成器如何节省内存",
        "source": "user_document",
        "expected_keywords": ["生成器", "yield"],
        "description": "用户文档 structure_aware",
    },
]


async def build_eval_index(
    persist_dir: Optional[str] = None,
) -> Tuple[UnifiedKnowledgeStore, List[str]]:
    """
    构建评测用索引

    Returns:
        store: UnifiedKnowledgeStore 实例
        uploaded_doc_ids: 用户文档的 document_id 列表
    """
    if persist_dir is None:
        persist_dir = tempfile.mkdtemp(prefix="retrieval_eval_")
    else:
        os.makedirs(persist_dir, exist_ok=True)

    embedding_model = create_embedding_model(use_local_embedding=True)
    if isinstance(embedding_model, TFIDFModel) and not embedding_model._fitted:
        # TF-IDF 需要先 fit，否则返回零向量
        all_texts = [item["content"] for item in COURSE_CORPUS]
        all_texts.extend([USER_DOC_ALGORITHMS, USER_DOC_PYTHON])
        embedding_model.fit(all_texts)
        logger.info("TF-IDF embedding model fitted on evaluation corpus")

    store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        collection_name="retrieval_eval",
        persist_directory=persist_dir,
    )

    # 1. 添加课程知识
    course_items = [
        KnowledgeItem(
            id=item["id"],
            title=item["title"],
            content=item["content"],
            source="course",
            metadata={"source": "course", "document_id": item["id"]},
        )
        for item in COURSE_CORPUS
    ]
    store.add_batch(course_items)

    # 2. 上传用户文档（一路 parent-child，一路 structure_aware）
    uploader_pc = DocumentUploader(
        knowledge_store=store,
        chunking_strategy="parent_child",
        parent_max_chars=500,
        child_max_chars=120,
        child_overlap_chars=20,
    )
    uploader_sa = DocumentUploader(
        knowledge_store=store,
        chunking_strategy="structure_aware",
    )

    result_pc = await uploader_pc.upload(
        content=USER_DOC_ALGORITHMS.encode("utf-8"),
        filename="algorithms.md",
        title="算法基础",
    )
    result_sa = await uploader_sa.upload(
        content=USER_DOC_PYTHON.encode("utf-8"),
        filename="python_tips.md",
        title="Python 技巧",
    )

    uploaded_doc_ids = [result_pc["document_id"], result_sa["document_id"]]
    logger.info(f"评测索引构建完成，持久化目录: {persist_dir}")
    return store, uploaded_doc_ids, persist_dir


def build_expected_index(
    store: UnifiedKnowledgeStore,
    queries: List[EvalCase],
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """
    根据关键词为每个评测查询构造期望文档 ID 集合

    返回两个索引：
    - chunk 级别：直接包含关键词的分块 ID（用于普通检索策略）
    - parent 级别：包含关键词的分块所属父块 ID（用于 parent-child 检索策略）
    """
    all_records = store.vector_store.get_all(
        limit=10000, include=["documents", "metadatas"]
    )
    records = []
    if all_records and all_records.get("ids"):
        for i, doc_id in enumerate(all_records["ids"]):
            records.append(
                {
                    "id": doc_id,
                    "content": (all_records.get("documents") or [""])[i],
                    "metadata": (all_records.get("metadatas") or [{}])[i],
                }
            )

    expected_index: Dict[str, List[str]] = {}
    expected_parent_index: Dict[str, List[str]] = {}
    for case in queries:
        keywords = case["expected_keywords"]
        source = case.get("source")
        matched_chunk_ids = []
        matched_parent_ids = set()
        for rec in records:
            if source and rec["metadata"].get("source") != source:
                continue
            content = rec["content"]
            if any(kw in content for kw in keywords):
                matched_chunk_ids.append(rec["id"])
                parent_id = rec["metadata"].get("parent_id", rec["id"])
                matched_parent_ids.add(parent_id)
        expected_index[case["query"]] = matched_chunk_ids
        expected_parent_index[case["query"]] = list(matched_parent_ids)
    return expected_index, expected_parent_index


# ========== 运行消融实验 ==========


async def _run_ablation_core(
    store: UnifiedKnowledgeStore,
    queries: List[EvalCase],
    k: int,
    persist_dir: Optional[str],
    cleanup: bool,
    rrf_k: int = 60,
    rewrite_query: bool = True,
    rewrite_mode: str = "basic",
    candidate_multiplier: int = 3,
    cross_max_length: int = 512,
) -> Dict[str, Any]:
    """消融实验核心逻辑"""
    expected_index, expected_parent_index = build_expected_index(store, queries)

    # 预先实例化 reranker，避免每次查询重复加载模型
    cross_reranker = CrossEncoderReranker(
        model_name="BAAI/bge-reranker-base",
        max_length=cross_max_length,
    )
    cross_reranker._load_model()

    # (方法名, 搜索函数, 是否使用 parent 级别期望索引)
    # 注：SimpleReranker 已验证效果差，已从默认策略中移除
    strategies: List[Tuple[str, SearchFn, bool]] = [
        ("vector_only", vector_only_search, False),
        ("bm25_only", bm25_only_search, False),
        ("hybrid_rrf", hybrid_rrf_search, False),
        ("hybrid_rrf+cross", make_hybrid_rrf_cross_search(cross_reranker), False),
        ("hybrid_rrf_pc", hybrid_rrf_pc_search, True),
        ("hybrid_rrf_pc+cross", make_hybrid_rrf_pc_cross_search(cross_reranker), True),
    ]

    evaluator = RetrievalEvaluator(k=k)
    summary: Dict[str, Dict[str, float]] = {}
    per_query: Dict[str, Dict[str, Dict[str, float]]] = {}

    for method_name, search_fn, use_parent_expected in strategies:
        method_scores = []
        per_query[method_name] = {}
        for case in queries:
            query = case["query"]
            source = case.get("source")
            expected_ids = (
                expected_parent_index if use_parent_expected else expected_index
            ).get(query, [])

            retrieved_ids = search_fn(
                store, query, source, k, rrf_k,
                rewrite_query=rewrite_query,
                rewrite_mode=rewrite_mode,
                candidate_multiplier=candidate_multiplier,
            )
            scores = evaluator.evaluate_case(retrieved_ids, expected_ids)
            method_scores.append(scores)
            per_query[method_name][query] = scores

        summary[method_name] = evaluator.aggregate(method_scores)

    if cleanup:
        del store
        shutil.rmtree(persist_dir, ignore_errors=True)

    return {"summary": summary, "per_query": per_query}


async def run_ablation(
    k: int = 5,
    persist_dir: Optional[str] = None,
    cleanup: bool = True,
    rrf_k: int = 60,
    rewrite_query: bool = True,
    rewrite_mode: str = "basic",
    candidate_multiplier: int = 3,
    cross_max_length: int = 512,
) -> Dict[str, Any]:
    """
    运行内置评测集的检索消融实验

    Returns:
        {
            "summary": {method: {metric: score}},
            "per_query": {method: {query: {metric: score}}},
        }
    """
    store, _, persist_dir = await build_eval_index(persist_dir)
    return await _run_ablation_core(
        store, EVAL_QUERIES, k, persist_dir, cleanup, rrf_k,
        rewrite_query=rewrite_query,
        rewrite_mode=rewrite_mode,
        candidate_multiplier=candidate_multiplier,
        cross_max_length=cross_max_length,
    )


def load_file_queries(queries_path: str) -> List[EvalCase]:
    """从 JSON 文件加载评测查询"""
    with open(queries_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("queries", [])


async def build_eval_index_from_files(
    docs_dir: str,
    persist_dir: Optional[str] = None,
    chunking_strategy: str = "parent_child",
    parent_max_chars: int = 800,
    child_max_chars: int = 160,
    child_overlap_chars: int = 30,
) -> Tuple[UnifiedKnowledgeStore, List[str]]:
    """
    从本地 Markdown 文件构建评测用索引

    Returns:
        store: UnifiedKnowledgeStore 实例
        uploaded_doc_ids: 用户文档的 document_id 列表
    """
    if persist_dir is None:
        persist_dir = tempfile.mkdtemp(prefix="retrieval_eval_files_")
    else:
        os.makedirs(persist_dir, exist_ok=True)

    doc_paths = sorted(Path(docs_dir).glob("*.md"))
    if not doc_paths:
        raise ValueError(f"在 {docs_dir} 下未找到 Markdown 文件")

    # 预读取所有文本，用于 TF-IDF fit
    all_texts = [p.read_text(encoding="utf-8") for p in doc_paths]

    embedding_model = create_embedding_model(use_local_embedding=True)
    if isinstance(embedding_model, TFIDFModel) and not embedding_model._fitted:
        embedding_model.fit(all_texts)
        logger.info("TF-IDF embedding model fitted on file corpus")

    store = UnifiedKnowledgeStore(
        embedding_model=embedding_model,
        collection_name="retrieval_eval_files",
        persist_directory=persist_dir,
    )

    uploader = DocumentUploader(
        knowledge_store=store,
        chunking_strategy=chunking_strategy,
        parent_max_chars=parent_max_chars,
        child_max_chars=child_max_chars,
        child_overlap_chars=child_overlap_chars,
    )

    uploaded_doc_ids = []
    for path in doc_paths:
        content = path.read_bytes()
        result = await uploader.upload(
            content=content,
            filename=path.name,
            title=path.stem,
        )
        uploaded_doc_ids.append(result["document_id"])

    logger.info(f"文件评测索引构建完成: {len(doc_paths)} 个文档, 目录: {persist_dir}")
    return store, uploaded_doc_ids, persist_dir


async def run_file_ablation(
    docs_dir: str,
    queries_path: str,
    k: int = 5,
    persist_dir: Optional[str] = None,
    cleanup: bool = True,
    chunking_strategy: str = "parent_child",
    rrf_k: int = 60,
    parent_max_chars: int = 800,
    child_max_chars: int = 160,
    child_overlap_chars: int = 30,
    rewrite_query: bool = True,
    rewrite_mode: str = "basic",
    candidate_multiplier: int = 3,
    cross_max_length: int = 512,
) -> Dict[str, Any]:
    """
    从本地文件运行检索消融实验

    Returns:
        {
            "summary": {method: {metric: score}},
            "per_query": {method: {query: {metric: score}}},
        }
    """
    queries = load_file_queries(queries_path)
    if not queries:
        raise ValueError(f"未从 {queries_path} 加载到查询")

    store, _, persist_dir = await build_eval_index_from_files(
        docs_dir,
        persist_dir,
        chunking_strategy=chunking_strategy,
        parent_max_chars=parent_max_chars,
        child_max_chars=child_max_chars,
        child_overlap_chars=child_overlap_chars,
    )
    return await _run_ablation_core(
        store, queries, k, persist_dir, cleanup, rrf_k,
        rewrite_query=rewrite_query,
        rewrite_mode=rewrite_mode,
        candidate_multiplier=candidate_multiplier,
        cross_max_length=cross_max_length,
    )


async def run_ablation_grid(
    docs_dir: Optional[str] = None,
    queries_path: Optional[str] = None,
    top_ks: List[int] = None,
    rrf_ks: List[int] = None,
    chunking_strategy: str = "parent_child",
    rewrite_mode: str = "basic",
) -> List[Dict[str, Any]]:
    """
    检索链路网格消融实验

    对比 top_k、rrf_k 对 hybrid_rrf+cross 效果的影响。
    """
    top_ks = top_ks or [5, 8, 10]
    rrf_ks = rrf_ks or [20, 40, 60, 80]

    def _normalize(scores: Dict[str, float], k: int) -> Dict[str, float]:
        """把 recall@5 / ndcg@5 等统一为 recall / ndcg，方便跨 k 对比"""
        return {
            key.replace(f"@{k}", ""): value
            for key, value in scores.items()
        }

    results = []
    for k in top_ks:
        for rrf_k in rrf_ks:
            logger.info(f"开始检索消融: top_k={k}, rrf_k={rrf_k}")
            if docs_dir and queries_path:
                result = await run_file_ablation(
                    docs_dir=docs_dir,
                    queries_path=queries_path,
                    k=k,
                    rrf_k=rrf_k,
                    chunking_strategy=chunking_strategy,
                    rewrite_mode=rewrite_mode,
                )
            else:
                result = await run_ablation(
                    k=k, rrf_k=rrf_k, rewrite_mode=rewrite_mode
                )
            summary = result["summary"]
            results.append({
                "top_k": k,
                "rrf_k": rrf_k,
                "hybrid_rrf": _normalize(summary.get("hybrid_rrf", {}), k),
                "hybrid_rrf+cross": _normalize(summary.get("hybrid_rrf+cross", {}), k),
            })
    return results


async def run_chunking_ablation(
    docs_dir: Optional[str] = None,
    queries_path: Optional[str] = None,
    chunking_configs: List[Tuple[int, int, int]] = None,
    top_k: int = 5,
    rrf_k: int = 40,
    rewrite_query: bool = True,
    rewrite_mode: str = "basic",
    candidate_multiplier: int = 3,
) -> List[Dict[str, Any]]:
    """
    分块参数消融实验

    对比不同 parent_max_chars / child_max_chars 对检索效果的影响。
    """
    chunking_configs = chunking_configs or [
        (600, 120, 20),
        (800, 160, 30),
        (1200, 200, 40),
    ]

    def _normalize(scores: Dict[str, float], k: int) -> Dict[str, float]:
        return {
            key.replace(f"@{k}", ""): value
            for key, value in scores.items()
        }

    results = []
    for parent_max_chars, child_max_chars, child_overlap_chars in chunking_configs:
        logger.info(
            f"开始分块消融: parent={parent_max_chars}, "
            f"child={child_max_chars}, overlap={child_overlap_chars}"
        )
        if docs_dir and queries_path:
            result = await run_file_ablation(
                docs_dir=docs_dir,
                queries_path=queries_path,
                k=top_k,
                rrf_k=rrf_k,
                parent_max_chars=parent_max_chars,
                child_max_chars=child_max_chars,
                child_overlap_chars=child_overlap_chars,
                rewrite_query=rewrite_query,
                rewrite_mode=rewrite_mode,
                candidate_multiplier=candidate_multiplier,
            )
        else:
            result = await run_ablation(
                k=top_k,
                rrf_k=rrf_k,
                rewrite_query=rewrite_query,
                rewrite_mode=rewrite_mode,
                candidate_multiplier=candidate_multiplier,
            )
        summary = result["summary"]
        results.append({
            "parent_max_chars": parent_max_chars,
            "child_max_chars": child_max_chars,
            "child_overlap_chars": child_overlap_chars,
            "hybrid_rrf": _normalize(summary.get("hybrid_rrf", {}), top_k),
            "hybrid_rrf+cross": _normalize(summary.get("hybrid_rrf+cross", {}), top_k),
        })
    return results


def print_chunking_ablation(results: List[Dict[str, Any]]) -> None:
    """打印分块参数消融结果"""
    print("\n" + "=" * 100)
    print("分块参数消融 (parent_max_chars x child_max_chars)")
    print("=" * 100)

    metrics = list(results[0]["hybrid_rrf+cross"].keys())
    header = (
        f"{'parent':<10}{'child':<10}{'overlap':<10}"
        + "".join(f"{m:<14}" for m in metrics)
    )
    print(header)
    print("-" * len(header))

    for item in results:
        row = (
            f"{item['parent_max_chars']:<10}"
            f"{item['child_max_chars']:<10}"
            f"{item['child_overlap_chars']:<10}"
        )
        row += "".join(
            f"{item['hybrid_rrf+cross'][m]:<14.3f}" for m in metrics
        )
        print(row)

    # 找出最优配置（按 ndcg）
    best_idx = max(
        range(len(results)),
        key=lambda i: results[i]["hybrid_rrf+cross"]["ndcg"],
    )
    best = results[best_idx]
    print(
        f"\n最优配置 (NDCG): "
        f"parent={best['parent_max_chars']}, "
        f"child={best['child_max_chars']}, "
        f"overlap={best['child_overlap_chars']}"
    )


async def run_advanced_ablation(
    docs_dir: Optional[str] = None,
    queries_path: Optional[str] = None,
    rewrite_options: List[bool] = None,
    candidate_multipliers: List[int] = None,
    cross_max_lengths: List[int] = None,
    top_k: int = 5,
    rrf_k: int = 40,
    rewrite_mode: str = "basic",
) -> List[Dict[str, Any]]:
    """
    高级检索参数消融实验

    对比 rewrite_query、candidate_multiplier、cross_encoder_max_length 的影响。
    """
    rewrite_options = rewrite_options or [True, False]
    candidate_multipliers = candidate_multipliers or [2, 3, 4]
    cross_max_lengths = cross_max_lengths or [512]

    results = []
    for rewrite_query in rewrite_options:
        for candidate_multiplier in candidate_multipliers:
            for cross_max_length in cross_max_lengths:
                logger.info(
                    f"开始高级检索消融: "
                    f"rewrite={rewrite_query}, "
                    f"multiplier={candidate_multiplier}, "
                    f"cross_max_length={cross_max_length}"
                )
                if docs_dir and queries_path:
                    result = await run_file_ablation(
                        docs_dir=docs_dir,
                        queries_path=queries_path,
                        k=top_k,
                        rrf_k=rrf_k,
                        rewrite_query=rewrite_query,
                        rewrite_mode=rewrite_mode,
                        candidate_multiplier=candidate_multiplier,
                        cross_max_length=cross_max_length,
                    )
                else:
                    result = await run_ablation(
                        k=top_k,
                        rrf_k=rrf_k,
                        rewrite_query=rewrite_query,
                        rewrite_mode=rewrite_mode,
                        candidate_multiplier=candidate_multiplier,
                        cross_max_length=cross_max_length,
                    )
                summary = result["summary"]

                def _normalize(scores: Dict[str, float], k: int) -> Dict[str, float]:
                    return {
                        key.replace(f"@{k}", ""): value
                        for key, value in scores.items()
                    }

                results.append({
                    "rewrite_query": rewrite_query,
                    "candidate_multiplier": candidate_multiplier,
                    "cross_max_length": cross_max_length,
                    "hybrid_rrf": _normalize(summary.get("hybrid_rrf", {}), top_k),
                    "hybrid_rrf+cross": _normalize(summary.get("hybrid_rrf+cross", {}), top_k),
                })
    return results


def print_advanced_ablation(results: List[Dict[str, Any]]) -> None:
    """打印高级检索参数消融结果"""
    print("\n" + "=" * 110)
    print("高级检索参数消融 (rewrite_query x candidate_multiplier x cross_max_length)")
    print("=" * 110)

    metrics = list(results[0]["hybrid_rrf+cross"].keys())
    header = (
        f"{'rewrite':<10}{'multiplier':<12}{'cross_len':<12}"
        + "".join(f"{m:<14}" for m in metrics)
    )
    print(header)
    print("-" * len(header))

    for item in results:
        row = (
            f"{str(item['rewrite_query']):<10}"
            f"{item['candidate_multiplier']:<12}"
            f"{item['cross_max_length']:<12}"
        )
        row += "".join(
            f"{item['hybrid_rrf+cross'][m]:<14.3f}" for m in metrics
        )
        print(row)

    # 找出最优配置（按 ndcg）
    best_idx = max(
        range(len(results)),
        key=lambda i: results[i]["hybrid_rrf+cross"]["ndcg"],
    )
    best = results[best_idx]
    print(
        f"\n最优配置 (NDCG): "
        f"rewrite={best['rewrite_query']}, "
        f"multiplier={best['candidate_multiplier']}, "
        f"cross_max_length={best['cross_max_length']}"
    )


def print_ablation_grid(results: List[Dict[str, Any]]) -> None:
    """打印网格消融结果"""
    print("\n" + "=" * 90)
    print("检索链路网格消融 (top_k x rrf_k)")
    print("=" * 90)

    metrics = list(results[0]["hybrid_rrf+cross"].keys())
    header = f"{'top_k':<8}{'rrf_k':<8}" + "".join(f"{m:<14}" for m in metrics)
    print(header)
    print("-" * len(header))

    for item in results:
        row = f"{item['top_k']:<8}{item['rrf_k']:<8}"
        row += "".join(
            f"{item['hybrid_rrf+cross'][m]:<14.3f}" for m in metrics
        )
        print(row)

    # 找出最优配置（按 ndcg）
    best_idx = max(
        range(len(results)),
        key=lambda i: results[i]["hybrid_rrf+cross"]["ndcg"],
    )
    best = results[best_idx]
    print(f"\n最优配置 (NDCG): top_k={best['top_k']}, rrf_k={best['rrf_k']}")


def print_results(results: Dict[str, Any], k: int) -> None:
    summary = results["summary"]
    per_query = results["per_query"]

    print("\n" + "=" * 80)
    print(f"检索消融实验结果 (K={k})")
    print("=" * 80)

    header = f"{'method':<25} " + " ".join(f"{m:<12}" for m in summary["vector_only"].keys())
    print(header)
    print("-" * len(header))
    for method, scores in summary.items():
        row = f"{method:<25} " + " ".join(f"{v:<12.3f}" for v in scores.values())
        print(row)

    print("\n各查询详细结果：")
    for method, queries in per_query.items():
        print(f"\n[{method}]")
        for query, scores in queries.items():
            score_str = ", ".join(f"{k}={v:.3f}" for k, v in scores.items())
            print(f"  {query}: {score_str}")


async def main():
    parser = argparse.ArgumentParser(description="检索效果消融实验")
    parser.add_argument(
        "--docs-dir",
        type=str,
        default=None,
        help="评测文档目录（Markdown 文件），不指定则使用内置数据集",
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=None,
        help="评测查询 JSON 文件，需与 --docs-dir 一起使用",
    )
    parser.add_argument("--k", type=int, default=5, help="评估 top-k")
    parser.add_argument(
        "--rrf-k",
        type=int,
        default=60,
        help="RRF 融合参数 k",
    )
    parser.add_argument(
        "--ablation-grid",
        action="store_true",
        help="运行 top_k x rrf_k 网格消融实验",
    )
    parser.add_argument(
        "--top-ks",
        type=int,
        nargs="+",
        default=[5, 8, 10],
        help="网格消融的 top_k 取值列表",
    )
    parser.add_argument(
        "--rrf-ks",
        type=int,
        nargs="+",
        default=[20, 40, 60, 80],
        help="网格消融的 rrf_k 取值列表",
    )
    parser.add_argument(
        "--chunking-ablation",
        action="store_true",
        help="运行 parent/child 分块参数消融实验",
    )
    parser.add_argument(
        "--advanced-ablation",
        action="store_true",
        help="运行 rewrite_query x candidate_multiplier x cross_max_length 消融实验",
    )
    parser.add_argument(
        "--rewrite-query",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否启用查询重写（默认开启）",
    )
    parser.add_argument(
        "--rewrite-mode",
        type=str,
        default="basic",
        choices=["basic", "enhanced", "llm", "enhanced_llm"],
        help="查询重写模式：basic / enhanced / llm（仅LLM）/ enhanced_llm（规则+LLM叠加）",
    )
    parser.add_argument(
        "--candidate-multiplier",
        type=int,
        default=3,
        help="vector/BM25 候选数量相对于 top_k 的倍数",
    )
    parser.add_argument(
        "--cross-max-length",
        type=int,
        default=512,
        help="CrossEncoder 重排时文档内容截断长度",
    )
    parser.add_argument(
        "--candidate-multipliers",
        type=int,
        nargs="+",
        default=None,
        help="高级消融的 candidate_multiplier 取值列表",
    )
    parser.add_argument(
        "--cross-max-lengths",
        type=int,
        nargs="+",
        default=None,
        help="高级消融的 cross_max_length 取值列表",
    )
    args = parser.parse_args()

    if args.ablation_grid:
        if args.docs_dir or args.queries:
            if not args.docs_dir or not args.queries:
                parser.error("--docs-dir 和 --queries 必须同时指定")
            results = await run_ablation_grid(
                docs_dir=args.docs_dir,
                queries_path=args.queries,
                top_ks=args.top_ks,
                rrf_ks=args.rrf_ks,
                rewrite_mode=args.rewrite_mode,
            )
        else:
            results = await run_ablation_grid(
                top_ks=args.top_ks,
                rrf_ks=args.rrf_ks,
                rewrite_mode=args.rewrite_mode,
            )
        print_ablation_grid(results)
    elif args.chunking_ablation:
        if args.docs_dir or args.queries:
            if not args.docs_dir or not args.queries:
                parser.error("--docs-dir 和 --queries 必须同时指定")
            results = await run_chunking_ablation(
                docs_dir=args.docs_dir,
                queries_path=args.queries,
                rewrite_mode=args.rewrite_mode,
            )
        else:
            results = await run_chunking_ablation(
                rewrite_mode=args.rewrite_mode,
            )
        print_chunking_ablation(results)
    elif args.advanced_ablation:
        if args.docs_dir or args.queries:
            if not args.docs_dir or not args.queries:
                parser.error("--docs-dir 和 --queries 必须同时指定")
            results = await run_advanced_ablation(
                docs_dir=args.docs_dir,
                queries_path=args.queries,
                candidate_multipliers=args.candidate_multipliers,
                cross_max_lengths=args.cross_max_lengths,
                rewrite_mode=args.rewrite_mode,
            )
        else:
            results = await run_advanced_ablation(
                candidate_multipliers=args.candidate_multipliers,
                cross_max_lengths=args.cross_max_lengths,
                rewrite_mode=args.rewrite_mode,
            )
        print_advanced_ablation(results)
    elif args.docs_dir or args.queries:
        if not args.docs_dir or not args.queries:
            parser.error("--docs-dir 和 --queries 必须同时指定")
        results = await run_file_ablation(
            docs_dir=args.docs_dir,
            queries_path=args.queries,
            k=args.k,
            rrf_k=args.rrf_k,
            rewrite_query=args.rewrite_query,
            rewrite_mode=args.rewrite_mode,
            candidate_multiplier=args.candidate_multiplier,
            cross_max_length=args.cross_max_length,
        )
        print_results(results, k=args.k)
    else:
        results = await run_ablation(
            k=args.k,
            rrf_k=args.rrf_k,
            rewrite_query=args.rewrite_query,
            rewrite_mode=args.rewrite_mode,
            candidate_multiplier=args.candidate_multiplier,
            cross_max_length=args.cross_max_length,
        )
        print_results(results, k=args.k)


if __name__ == "__main__":
    asyncio.run(main())
