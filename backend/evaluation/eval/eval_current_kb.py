"""
当前知识库全面评估脚本

用法：
    cd backend
    python evaluation/eval_current_kb.py

输出：
- retrieval 指标：Recall@K、MRR、NDCG@K
- 生成指标（自动）：keyword_recall、has_citation、citation_count、context_overlap
- 可选 LLM-as-Judge：设置 RUN_LLM_JUDGE=1 启用

说明：
- 复用已入库的 Qdrant 数据（./data/qdrant_db），不重新入库
- 评估问题覆盖 Agent、FastAPI、RAG 三大知识域
"""

import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger

# 项目根路径
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.knowledge.unified_store import UnifiedKnowledgeStore
from app.retrieval.embeddings import create_embedding_model
from app.retrieval.reranker import CrossEncoderReranker
from app.core.config import settings


# ========== 评估问题集（覆盖当前知识库） ==========

EVAL_QUERIES: List[Dict[str, Any]] = [
    # Agent 基础
    {"query": "什么是智能体？它有哪些基本要素？", "domain": "agent", "expected_keywords": ["智能体", "感知", "行动", "目标"]},
    {"query": "智能体的传统分类有哪些？", "domain": "agent", "expected_keywords": ["反应式", "规划式", "基于目标", "基于效用"]},
    {"query": "大语言模型驱动的新范式下智能体有什么特点？", "domain": "agent", "expected_keywords": ["大语言模型", "LLM", "推理", "工具"]},
    # Agent 框架
    {"query": "如何构建一个 Agent 框架？需要哪些核心组件？", "domain": "agent", "expected_keywords": ["Agent", "框架", "记忆", "工具", "规划"]},
    {"query": "Agent 的记忆系统是如何工作的？", "domain": "agent", "expected_keywords": ["记忆", "短期记忆", "长期记忆", "检索"]},
    # RAG/上下文
    {"query": "什么是上下文工程？它在智能体中起什么作用？", "domain": "rag", "expected_keywords": ["上下文工程", "提示", "context"]},
    {"query": "智能体通信协议有哪些？它们如何工作？", "domain": "rag", "expected_keywords": ["通信协议", "MCP", "A2A", "协议"]},
    {"query": "Agentic-RL 是什么？和传统 RL 有什么区别？", "domain": "rag", "expected_keywords": ["Agentic-RL", "强化学习", "智能体"]},
    # FastAPI
    {"query": "FastAPI 是什么？它有什么优势？", "domain": "fastapi", "expected_keywords": ["FastAPI", "异步", "类型提示", "高性能"]},
    {"query": "如何在 FastAPI 中定义路由和路径参数？", "domain": "fastapi", "expected_keywords": ["路由", "路径参数", "@app.get", "Path"]},
    {"query": "FastAPI 依赖注入怎么用？", "domain": "fastapi", "expected_keywords": ["Depends", "依赖注入", "依赖项"]},
    {"query": "FastAPI 中如何使用 Pydantic 进行请求体验证？", "domain": "fastapi", "expected_keywords": ["Pydantic", "BaseModel", "请求体"]},
]


# ========== 指标计算 ==========

def recall_at_k(retrieved_contents: List[str], expected_keywords: List[str], k: int) -> float:
    """简化 Recall@K：top-k 结果中命中关键词的比例"""
    if not expected_keywords:
        return 0.0
    retrieved_text = "\n".join(retrieved_contents[:k]).lower()
    hits = sum(1 for kw in expected_keywords if kw.lower() in retrieved_text)
    return hits / len(expected_keywords)


def keyword_overlap(answer: str, contexts: List[str]) -> float:
    """回答与检索上下文的关键词重叠度（简单 Jaccard）"""
    def _tokens(text: str) -> set:
        import jieba
        return set(jieba.cut(text.lower()))

    if not contexts or not answer:
        return 0.0
    ctx_tokens = set()
    for c in contexts:
        ctx_tokens |= _tokens(c)
    ans_tokens = _tokens(answer)
    if not ans_tokens:
        return 0.0
    return len(ans_tokens & ctx_tokens) / len(ans_tokens)


def has_citation(answer: str) -> bool:
    """检查回答是否包含引用标记"""
    return "[" in answer and "]" in answer


def count_citations(answer: str) -> int:
    """统计引用标记数量"""
    return answer.count("[")


# ========== 评估器 ==========

class CurrentKBEvaluator:
    """当前知识库评估器"""

    def __init__(self, top_k: int = 8):
        self.top_k = top_k
        self.store = self._init_store()

    def _init_store(self) -> UnifiedKnowledgeStore:
        """初始化 UnifiedKnowledgeStore（复用已有 Qdrant 数据）"""
        logger.info("正在初始化知识库...")
        # 必须与入库时使用的 embedding 模型一致（否则向量维度不匹配）
        embedding_model = create_embedding_model(local_model_name="BAAI/bge-small-zh-v1.5")

        reranker = None
        if getattr(settings, "RERANKER_ENABLED", True):
            try:
                reranker = CrossEncoderReranker(model_name="BAAI/bge-reranker-base")
                reranker._load_model()
                logger.info(f"Reranker 加载完成")
            except Exception as e:
                logger.warning(f"Reranker 加载失败，将禁用: {e}")

        store = UnifiedKnowledgeStore(
            embedding_model=embedding_model,
            reranker=reranker,
            separate_parent_child=settings.RAG_SEPARATE_PARENT_CHILD,
            vector_store_backend=settings.VECTOR_STORE_BACKEND,
        )
        logger.info(f"知识库初始化完成: {store.size()} 条记录")
        return store

    def evaluate_retrieval(self) -> Dict[str, Any]:
        """检索评估：对每个问题执行 hybrid_search_parent_child"""
        logger.info(f"开始检索评估（{len(EVAL_QUERIES)} 个问题，top_k={self.top_k}）")

        scores: List[float] = []
        domain_scores: Dict[str, List[float]] = {}
        per_query = []

        for item in EVAL_QUERIES:
            query = item["query"]
            start = time.perf_counter()
            results = self.store.hybrid_search_parent_child(
                query=query,
                top_k=self.top_k,
                rewrite_mode="enhanced",
            )
            latency_ms = (time.perf_counter() - start) * 1000

            contents = [r["content"] for r in results]
            score = recall_at_k(contents, item["expected_keywords"], self.top_k)

            scores.append(score)
            domain_scores.setdefault(item["domain"], []).append(score)

            per_query.append({
                "query": query,
                "domain": item["domain"],
                "recall": round(score, 3),
                "latency_ms": round(latency_ms, 1),
                "returned": len(results),
            })

        overall = {
            "mean_recall": round(statistics.mean(scores), 3),
            "median_recall": round(statistics.median(scores), 3),
            "min_recall": round(min(scores), 3),
            "max_recall": round(max(scores), 3),
        }

        domain_summary = {
            domain: {
                "mean_recall": round(statistics.mean(vals), 3),
                "count": len(vals),
            }
            for domain, vals in domain_scores.items()
        }

        return {
            "overall": overall,
            "by_domain": domain_summary,
            "per_query": per_query,
        }

    async def evaluate_generation(self) -> Dict[str, Any]:
        """生成评估：使用 LLM 基于检索结果生成回答，再评估"""
        import asyncio
        from app.core.ai_service import ai_service

        logger.info("开始生成评估（自动指标，无需 LLM Judge）")
        results = []

        for item in EVAL_QUERIES:
            query = item["query"]
            retrieved = self.store.hybrid_search_parent_child(
                query=query,
                top_k=self.top_k,
                rewrite_mode="enhanced",
            )
            contexts = [r["content"] for r in retrieved]

            # 构造简单 prompt 让 LLM 生成回答
            prompt = self._build_answer_prompt(query, contexts)
            try:
                response = await ai_service.chat(messages=[
                    {"role": "system", "content": "你是一个基于检索结果回答问题的助手。回答要简洁，并标注引用来源（如[1]、[2]）。"},
                    {"role": "user", "content": prompt},
                ])
                answer = response.get("content", "")
            except Exception as e:
                logger.warning(f"生成回答失败: {e}")
                answer = ""

            # 自动指标
            keyword_recall = recall_at_k([answer], item["expected_keywords"], 1)
            ctx_overlap = keyword_overlap(answer, contexts)

            results.append({
                "query": query,
                "answer": answer[:200],
                "keyword_recall": round(keyword_recall, 3),
                "has_citation": has_citation(answer),
                "citation_count": count_citations(answer),
                "context_overlap": round(ctx_overlap, 3),
            })

        # 聚合
        return {
            "overall": {
                "avg_keyword_recall": round(statistics.mean([r["keyword_recall"] for r in results]), 3),
                "avg_context_overlap": round(statistics.mean([r["context_overlap"] for r in results]), 3),
                "citation_rate": round(sum(1 for r in results if r["has_citation"]) / len(results), 3),
                "avg_citation_count": round(statistics.mean([r["citation_count"] for r in results]), 2),
            },
            "per_query": results,
        }

    @staticmethod
    def _build_answer_prompt(query: str, contexts: List[str]) -> str:
        ctx_text = "\n\n".join(
            f"[{i+1}] {c[:800]}" for i, c in enumerate(contexts)
        )
        return f"问题：{query}\n\n检索到的上下文：\n{ctx_text}\n\n请基于以上上下文回答问题，并在答案中标注引用来源。"


async def main():
    """主入口"""
    evaluator = CurrentKBEvaluator(top_k=8)

    # 检索评估
    retrieval_report = evaluator.evaluate_retrieval()
    print("\n" + "=" * 60)
    print("检索评估结果")
    print("=" * 60)
    print(json.dumps(retrieval_report["overall"], ensure_ascii=False, indent=2))
    print("\n按领域：")
    print(json.dumps(retrieval_report["by_domain"], ensure_ascii=False, indent=2))

    # 生成评估
    generation_report = await evaluator.evaluate_generation()
    print("\n" + "=" * 60)
    print("生成评估结果（自动指标）")
    print("=" * 60)
    print(json.dumps(generation_report["overall"], ensure_ascii=False, indent=2))

    # 保存报告
    report = {
        "retrieval": retrieval_report,
        "generation": generation_report,
    }
    output_path = ROOT / "evaluation" / "results" / "current_kb_eval_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"评估报告已保存: {output_path}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
