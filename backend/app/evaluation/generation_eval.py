"""
生成效果评测工具

评估 RAG 生成链路在检索上下文基础上的答案质量，支持：
- 关键词召回（答案覆盖期望关键词的比例）
- 引用检测（是否使用 [1]、[2] 等引用标记）
- 上下文重叠度（答案与检索上下文的词重叠，间接反映幻觉）
- LLM-as-a-Judge（正确性、完整性、相关性、幻觉，可选）

用法（PowerShell）：
    python -m app.evaluation.generation_eval
    $env:RUN_LLM_JUDGE='1'; python -m app.evaluation.generation_eval
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core.ai_service import MockProvider, create_ai_provider
from app.evaluation.retrieval_eval import build_eval_index_from_files, load_file_queries
from app.memory.memory_manager import MemoryManager
from app.shared_services import RAGRetrieverAdapter


async def _generate_answer(
    memory: MemoryManager,
    llm,
    query: str,
    max_rag_results: int = 5,
    rag_content_limit: int = 400,
    extra_instructions: Optional[str] = None,
) -> str:
    """轻量 RAG 生成：build_context + LLM 调用 + 引用拼接

    原 RAGGenerator 的核心生成逻辑内联（仅保留评测所需 use_rag 路径，
    移除 web_search / memory_update / save_to_memory 等未用功能）。
    """
    context = memory.build_context(
        query=query,
        user_id="eval_user",
        session_id="eval_session",
        include_core=False,
        include_recall=False,
        include_archival=False,
        include_rag=True,
        max_rag_results=max_rag_results,
        rag_content_limit=rag_content_limit,
    )

    extra = f"\n8. {extra_instructions}\n" if extra_instructions else ""
    prompt = f"""{context}

## 用户问题
{query}

## 要求
1. 基于提供的上下文回答问题
2. 如果是追问，理解对话历史的上下文
3. **重要**：如果用户问的是"刚才问了什么"、"我们聊了什么"等关于对话历史的问题，请只基于对话历史部分回答，不需要引用知识库或网络搜索结果
4. 引用知识库来源时使用 [1]、[2] 等标记
5. 如果上下文没有相关信息，明确说明
6. 保持回答简洁、准确、有帮助
7. 回答中应明确覆盖用户问题里的关键概念，如技术术语、命令、关键字等，并对每个关键概念给出具体说明，避免只给出简要结论{extra}

## 回答"""

    try:
        messages = [
            {"role": "system", "content": "你是一个专业的知识助手，基于提供的上下文回答用户问题。"},
            {"role": "user", "content": prompt},
        ]
        if hasattr(llm, "generate"):
            answer = await llm.generate(prompt)
        elif hasattr(llm, "chat"):
            response = await llm.chat(messages)
            answer = response.get("content", "")
        else:
            answer = "抱歉，AI 服务不可用。"
    except Exception as e:
        logger.error(f"LLM 生成失败: {e}")
        answer = f"抱歉，生成答案时出现错误：{str(e)}"

    # 追加引用来源（与原 RAGGenerator._add_citations 一致）
    sources = memory.search_knowledge(query, max_rag_results)
    if sources:
        citations = []
        for i, source in enumerate(sources, 1):
            title = source.get("metadata", {}).get("title", "") or source.get("title", "")
            url = source.get("metadata", {}).get("url", "") or source.get("url", "")
            if title:
                if url:
                    citations.append(f"[{i}] [{title}]({url})")
                else:
                    citations.append(f"[{i}] {title}")
        if citations:
            answer += "\n\n---\n**引用来源：**\n" + "\n".join(citations)

    return answer


DOCS_DIR = Path(__file__).parent.parent.parent / "evaluation" / "data" / "documents"
QUERIES_PATH = Path(__file__).parent.parent.parent / "evaluation" / "data" / "queries.json"


class GenerationEvaluator:
    """生成效果评估器"""

    def __init__(self, enable_llm_judge: bool = False):
        self.llm_judge = None
        if enable_llm_judge:
            self.llm_judge = create_ai_provider()

    async def evaluate_case(
        self,
        query: str,
        answer: str,
        context: str,
        expected_keywords: List[str],
    ) -> Dict[str, float]:
        """对单个生成结果评分"""
        scores: Dict[str, float] = {}
        answer_lower = answer.lower()

        # 1. 关键词召回
        if expected_keywords:
            matched = sum(
                1 for kw in expected_keywords if kw.lower() in answer_lower
            )
            scores["keyword_recall"] = matched / len(expected_keywords)
        else:
            scores["keyword_recall"] = 0.0

        # 2. 引用检测
        citations = re.findall(r"\[\d+\]", answer)
        scores["has_citation"] = 1.0 if citations else 0.0
        scores["citation_count"] = float(len(citations))

        # 3. 上下文重叠度
        scores["context_overlap"] = self._context_overlap(answer, context)

        # 4. 答案长度
        scores["answer_length"] = float(len(answer))

        # 5. LLM-as-Judge
        if self.llm_judge and not isinstance(self.llm_judge, MockProvider):
            judge_scores = await self._llm_judge_scores(query, answer, context)
            scores.update(judge_scores)

        return scores

    def _context_overlap(self, answer: str, context: str) -> float:
        """答案与上下文的词集合重叠率"""
        if not context:
            return 0.0
        answer_tokens = set(self._tokenize(answer))
        context_tokens = set(self._tokenize(context))
        if not answer_tokens:
            return 0.0
        return len(answer_tokens & context_tokens) / len(answer_tokens)

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r"[a-zA-Z0-9_\u4e00-\u9fa5]+", text.lower())

    async def _llm_judge_scores(self, query: str, answer: str, context: str) -> Dict[str, float]:
        """使用 LLM 对答案多维度打分"""
        prompt = f"""你是一名严格的答案质量评估员。请根据上下文和用户问题，对以下答案从 4 个维度按 1-5 分打分，只输出 JSON：

{{
  "correctness": <int>,   // 答案事实是否正确
  "completeness": <int>,  // 是否完整回答用户问题
  "relevance": <int>,     // 是否与问题高度相关
  "hallucination": <int>  // 1=存在严重幻觉，5=无幻觉
}}

上下文：
{context[:2000]}

问题：{query}

答案：{answer}
"""
        try:
            response = await self.llm_judge.chat([{"role": "user", "content": prompt}])
            content = response.get("content", "{}")
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return {
                    "judge_correctness": float(data.get("correctness", 0)),
                    "judge_completeness": float(data.get("completeness", 0)),
                    "judge_relevance": float(data.get("relevance", 0)),
                    "judge_hallucination": float(data.get("hallucination", 0)),
                }
        except Exception as e:
            logger.warning(f"LLM judge 失败: {e}")
        return {}


async def run_generation_eval(
    docs_dir: Optional[str] = None,
    queries_path: Optional[str] = None,
    persist_dir: Optional[str] = None,
    cleanup: bool = True,
    enable_llm_judge: bool = False,
    rag_content_limit: int = 400,
    max_rag_results: int = 5,
    extra_instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """
    运行生成效果评测

    Returns:
        {
            "summary": {metric: mean_score},
            "per_query": [{query, answer, context, expected_keywords, scores}],
        }
    """
    docs_dir = str(docs_dir or DOCS_DIR)
    queries_path = str(queries_path or QUERIES_PATH)
    queries = load_file_queries(queries_path)

    store, _, persist_dir = await build_eval_index_from_files(
        docs_dir=docs_dir,
        persist_dir=persist_dir,
        chunking_strategy="parent_child",
    )

    try:
        rag_retriever = RAGRetrieverAdapter(store)
        memory = MemoryManager(
            embedding_model=store.embedding_model,
            vector_store=store.vector_store,
            rag_retriever=rag_retriever,
        )
        llm = create_ai_provider()

        evaluator = GenerationEvaluator(enable_llm_judge=enable_llm_judge)
        per_query_scores: List[Dict[str, float]] = []
        raw_results: List[Dict[str, Any]] = []

        for case in queries:
            query = case["query"]
            expected_keywords = case.get("expected_keywords", [])

            answer = await _generate_answer(
                memory=memory,
                llm=llm,
                query=query,
                max_rag_results=max_rag_results,
                rag_content_limit=rag_content_limit,
                extra_instructions=extra_instructions,
            )

            context = memory.build_context(
                query=query,
                user_id="eval_user",
                session_id="eval_session",
                include_core=False,
                include_recall=False,
                include_archival=False,
                include_rag=True,
                max_rag_results=max_rag_results,
                rag_content_limit=rag_content_limit,
            )

            scores = await evaluator.evaluate_case(
                query=query,
                answer=answer,
                context=context,
                expected_keywords=expected_keywords,
            )
            per_query_scores.append(scores)
            raw_results.append({
                "query": query,
                "answer": answer,
                "context": context,
                "expected_keywords": expected_keywords,
                "scores": scores,
            })

        summary = {
            metric: statistics.mean(
                s[metric] for s in per_query_scores if metric in s
            )
            for metric in per_query_scores[0].keys()
        }

        return {
            "summary": summary,
            "per_query": raw_results,
        }
    finally:
        if cleanup:
            shutil.rmtree(persist_dir, ignore_errors=True)


def print_results(results: Dict[str, Any]) -> None:
    """打印评测结果"""
    summary = results["summary"]
    per_query = results["per_query"]

    print("\n" + "=" * 80)
    print("生成效果评测结果")
    print("=" * 80)

    for metric, value in summary.items():
        print(f"  {metric:<20}: {value:.3f}")

    print("\n各查询详细结果：")
    for item in per_query:
        query = item["query"]
        scores = item["scores"]
        score_str = ", ".join(f"{k}={v:.2f}" for k, v in scores.items())
        print(f"  {query}: {score_str}")


async def run_ablation(
    docs_dir: Optional[str] = None,
    queries_path: Optional[str] = None,
    enable_llm_judge: bool = False,
) -> List[Dict[str, Any]]:
    """
    运行生成效果消融实验

    对比 prompt、上下文长度、检索 top-k 的组合效果。
    """
    keyword_instruction = (
        "回答中应明确覆盖用户问题里的关键概念，"
        "如技术术语、命令、关键字等。"
    )

    configs = [
        {
            "name": "baseline",
            "rag_content_limit": 400,
            "max_rag_results": 5,
            "extra_instructions": None,
        },
        {
            "name": "prompt_keywords",
            "rag_content_limit": 400,
            "max_rag_results": 5,
            "extra_instructions": keyword_instruction,
        },
        {
            "name": "long_context",
            "rag_content_limit": 800,
            "max_rag_results": 5,
            "extra_instructions": None,
        },
        {
            "name": "more_results",
            "rag_content_limit": 400,
            "max_rag_results": 8,
            "extra_instructions": None,
        },
        {
            "name": "combined",
            "rag_content_limit": 800,
            "max_rag_results": 8,
            "extra_instructions": keyword_instruction,
        },
    ]

    results = []
    for cfg in configs:
        logger.info(f"开始消融配置: {cfg['name']}")
        result = await run_generation_eval(
            docs_dir=docs_dir,
            queries_path=queries_path,
            enable_llm_judge=enable_llm_judge,
            rag_content_limit=cfg["rag_content_limit"],
            max_rag_results=cfg["max_rag_results"],
            extra_instructions=cfg["extra_instructions"],
        )
        results.append({
            "name": cfg["name"],
            "config": cfg,
            "summary": result["summary"],
            "per_query": result["per_query"],
        })

    return results


def print_ablation_results(results: List[Dict[str, Any]]) -> None:
    """打印消融实验对比表格"""
    print("\n" + "=" * 100)
    print("生成效果消融实验结果")
    print("=" * 100)

    # 表头
    metrics = list(results[0]["summary"].keys())
    header = f"{'config':<18}" + "".join(f"{m:<14}" for m in metrics)
    print(header)
    print("-" * len(header))

    for item in results:
        name = item["name"]
        summary = item["summary"]
        row = f"{name:<18}" + "".join(f"{summary[m]:<14.3f}" for m in metrics)
        print(row)

    print("\n配置说明：")
    for item in results:
        cfg = item["config"]
        print(
            f"  {item['name']:<18}: "
            f"limit={cfg['rag_content_limit']}, "
            f"top_k={cfg['max_rag_results']}, "
            f"extra={'有' if cfg['extra_instructions'] else '无'}"
        )


async def main():
    parser = argparse.ArgumentParser(description="生成效果评测")
    parser.add_argument(
        "--docs-dir",
        type=str,
        default=None,
        help="评测文档目录（Markdown 文件）",
    )
    parser.add_argument(
        "--queries",
        type=str,
        default=None,
        help="评测查询 JSON 文件",
    )
    parser.add_argument(
        "--llm-judge",
        action="store_true",
        help="启用 LLM-as-a-Judge（需配置 AI_API_KEY）",
    )
    parser.add_argument(
        "--rag-content-limit",
        type=int,
        default=400,
        help="RAG 上下文单条内容截断长度",
    )
    parser.add_argument(
        "--ablation",
        action="store_true",
        help="运行消融实验（对比多组 prompt/context/top-k 配置）",
    )
    parser.add_argument(
        "--extra-instructions",
        type=str,
        default=None,
        help="附加到默认 prompt 的额外要求，用于快速实验 prompt 优化",
    )
    args = parser.parse_args()

    enable_llm_judge = args.llm_judge or os.environ.get("RUN_LLM_JUDGE") == "1"

    if args.ablation:
        results = await run_ablation(
            docs_dir=args.docs_dir,
            queries_path=args.queries,
            enable_llm_judge=enable_llm_judge,
        )
        print_ablation_results(results)
    else:
        results = await run_generation_eval(
            docs_dir=args.docs_dir,
            queries_path=args.queries,
            enable_llm_judge=enable_llm_judge,
            rag_content_limit=args.rag_content_limit,
            extra_instructions=args.extra_instructions,
        )
        print_results(results)


if __name__ == "__main__":
    asyncio.run(main())
