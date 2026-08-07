"""
RAGAS 风格的 RAG 评估工具（轻量自实现版）

参考 RAGAS（Retrieval-Augmented Generation Assessment）框架的核心思想，
使用项目已有的 AI service 作为 LLM judge，自实现 4 个核心指标：

1. Faithfulness（忠实度）：答案是否忠实于检索上下文（检测幻觉）
   - LLM 将答案拆解为原子陈述 → 逐条验证是否可由上下文推断 → 可推断比例
2. Answer Relevancy（答案相关性）：答案是否切题
   - LLM 根据答案反向生成问题变体 → 与原始问题的 embedding 相似度 → 均值
3. Context Precision（上下文精确度）：检索结果中相关片段的比例（带排名加权）
   - LLM 逐条判断每个 context 是否与问题相关 → 加权比例
4. Context Recall（上下文召回率）：相关信息均被检索到的比例（需 ground_truth）
   - LLM 将 ground_truth 拆解为陈述 → 逐条验证是否可由上下文推断 → 覆盖比例

为什么自实现而不直接装 ragas 库：
- 中文场景：ragas 官方 prompt 针对英文，自实现可定制中文 prompt
- 轻依赖：ragas 依赖 langchain/langsmith 较重
- 无缝集成：复用项目已有 AI service / embedding model，与现有评估体系一致

用法（PowerShell）：
    python -m app.evaluation.ragas_eval
    $env:RUN_RAGAS='1'; python -m pytest tests/test_ragas_eval.py -v
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
) -> str:
    """轻量 RAG 生成：build_context + LLM 调用 + 引用拼接

    原 RAGGenerator 的核心生成逻辑内联（仅保留评测所需 use_rag 路径，
    移除 web_search / memory_update / save_to_memory 等未用功能）。
    """
    context = memory.build_context(
        query=query,
        user_id="ragas_eval_user",
        session_id="ragas_eval_session",
        include_core=False,
        include_recall=False,
        include_archival=False,
        include_rag=True,
        max_rag_results=max_rag_results,
        rag_content_limit=rag_content_limit,
    )

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
7. 回答中应明确覆盖用户问题里的关键概念，如技术术语、命令、关键字等，并对每个关键概念给出具体说明，避免只给出简要结论

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

    return answer


DOCS_DIR = Path(__file__).parent.parent.parent / "evaluation" / "data" / "documents"
QUERIES_PATH = Path(__file__).parent.parent.parent / "evaluation" / "data" / "queries.json"


# ========== 工具函数 ==========


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """计算两个向量的余弦相似度"""
    if not vec_a or not vec_b:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _llm_chat(llm, prompt: str) -> str:
    """调用 LLM 并返回文本内容，失败时返回空串"""
    if llm is None or isinstance(llm, MockProvider):
        return ""
    try:
        messages = [{"role": "user", "content": prompt}]
        response = await llm.chat(messages)
        return response.get("content", "").strip()
    except Exception as e:
        logger.warning(f"LLM 调用失败: {e}")
        return ""


def _parse_json_list(text: str) -> List[str]:
    """从 LLM 输出中解析 JSON 字符串数组，容错处理"""
    if not text:
        return []
    # 去掉 markdown 代码块标记
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    # 尝试直接解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x) for x in data]
    except json.JSONDecodeError:
        pass
    # 尝试提取第一个 JSON 数组
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    return []


def _parse_json_object(text: str) -> Dict[str, Any]:
    """从 LLM 输出中解析 JSON 对象"""
    if not text:
        return {}
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


# ========== RAGAS 指标实现 ==========


class RAGASEvaluator:
    """RAGAS 风格评估器"""

    def __init__(
        self,
        llm=None,
        embedding_model=None,
        faithfulness_concurrency: int = 4,
    ):
        """
        Args:
            llm: AI service provider（None 时用 create_ai_provider 创建）
            embedding_model: embedding 模型（用于 answer relevancy）
            faithfulness_concurrency: 陈述验证并发数
        """
        self.llm = llm or create_ai_provider()
        self.embedding_model = embedding_model
        self._sem = asyncio.Semaphore(faithfulness_concurrency)

    # ----- Faithfulness（忠实度）-----

    async def _extract_statements(self, answer: str) -> List[str]:
        """将答案拆解为原子陈述"""
        prompt = (
            "请将下面的答案拆解为若干条原子陈述（每条陈述一个独立的事实判断），"
            "只输出 JSON 字符串数组，不要任何解释。\n\n"
            f"答案：\n{answer}\n\n"
            '示例输出：["陈述1", "陈述2"]'
        )
        text = await _llm_chat(self.llm, prompt)
        statements = _parse_json_list(text)
        if not statements:
            # 降级：按句号切分
            statements = [s.strip() for s in re.split(r"[。.！!？?]", answer) if s.strip()]
        return statements

    async def _verify_statement(self, statement: str, context: str) -> bool:
        """验证单条陈述是否能从上下文推断"""
        async with self._sem:
            prompt = (
                "请判断以下陈述是否能从给定的上下文中推断出来（即上下文是否支持该陈述）。\n"
                "只输出 JSON 对象：{\"verdict\": \"yes\"} 或 {\"verdict\": \"no\"}\n\n"
                f"上下文：\n{context[:2000]}\n\n"
                f"陈述：{statement}"
            )
            text = await _llm_chat(self.llm, prompt)
            obj = _parse_json_object(text)
            return obj.get("verdict", "no").lower().startswith("y")

    async def faithfulness(self, answer: str, contexts: List[str]) -> float:
        """
        忠实度：答案中能被上下文支持的陈述比例

        Returns:
            0.0-1.0，越高越好
        """
        if not answer or not contexts:
            return 0.0
        context = "\n\n".join(contexts)
        statements = await self._extract_statements(answer)
        if not statements:
            return 0.0
        # 并发验证每条陈述
        tasks = [self._verify_statement(s, context) for s in statements]
        verdicts = await asyncio.gather(*tasks, return_exceptions=False)
        supported = sum(1 for v in verdicts if v)
        return supported / len(statements)

    # ----- Answer Relevancy（答案相关性）-----

    async def _generate_reverse_questions(self, question: str, answer: str, n: int = 3) -> List[str]:
        """根据答案反向生成 n 个问题变体"""
        prompt = (
            f"请根据下面的答案，推测它可能回答的问题。生成 {n} 个不同的问题变体，"
            "只输出 JSON 字符串数组。\n\n"
            f"原始问题（参考）：{question}\n"
            f"答案：\n{answer}\n\n"
            f'示例输出：["问题1", "问题2", "问题3"]'
        )
        text = await _llm_chat(self.llm, prompt)
        questions = _parse_json_list(text)
        return questions[:n]

    async def answer_relevancy(self, question: str, answer: str) -> float:
        """
        答案相关性：反向生成问题与原始问题的 embedding 相似度均值

        Returns:
            0.0-1.0，越高越好
        """
        if not answer or not question:
            return 0.0
        if self.embedding_model is None:
            logger.warning("未提供 embedding_model，answer_relevancy 降级为 LLM 直接评分")
            return await self._answer_relevancy_llm_fallback(question, answer)

        reverse_qs = await self._generate_reverse_questions(question, answer)
        if not reverse_qs:
            return 0.0

        # 用 embedding 计算相似度
        try:
            all_vecs = self.embedding_model.embed_batch([question] + reverse_qs)
            q_vec = all_vecs[0]
            sims = [_cosine_similarity(q_vec, v) for v in all_vecs[1:]]
            # 相似度可能为负，截断到 [0, 1]
            sims = [max(0.0, min(1.0, s)) for s in sims]
            return statistics.mean(sims)
        except Exception as e:
            logger.warning(f"answer_relevancy embedding 计算失败，降级 LLM: {e}")
            return await self._answer_relevancy_llm_fallback(question, answer)

    async def _answer_relevancy_llm_fallback(self, question: str, answer: str) -> float:
        """无 embedding 时用 LLM 直接打分（1-5 归一化到 0-1）"""
        prompt = (
            "请评估以下答案与问题的相关程度，按 1-5 打分（5=完全切题，1=严重跑题）。\n"
            "只输出 JSON 对象：{\"score\": <int>}\n\n"
            f"问题：{question}\n答案：{answer}"
        )
        text = await _llm_chat(self.llm, prompt)
        obj = _parse_json_object(text)
        score = float(obj.get("score", 0))
        return max(0.0, min(1.0, score / 5.0))

    # ----- Context Precision（上下文精确度）-----

    async def _judge_context_relevance(self, question: str, context: str) -> bool:
        """判断单个 context 是否与问题相关"""
        prompt = (
            "请判断以下检索到的文本片段对于回答用户问题是否有用。\n"
            "只输出 JSON 对象：{\"useful\": true} 或 {\"useful\": false}\n\n"
            f"问题：{question}\n"
            f"文本片段：\n{context[:1000]}"
        )
        text = await _llm_chat(self.llm, prompt)
        obj = _parse_json_object(text)
        return bool(obj.get("useful", False))

    async def context_precision(self, question: str, contexts: List[str]) -> float:
        """
        上下文精确度：相关片段的排名加权比例

        排名越靠前的相关片段权重越高（rank@k 的 DCG 风格加权）。

        Returns:
            0.0-1.0，越高越好
        """
        if not contexts:
            return 0.0
        # 并发判断每条 context 是否相关
        tasks = [self._judge_context_relevance(question, c) for c in contexts]
        verdicts = await asyncio.gather(*tasks, return_exceptions=False)

        # 加权：rank 越靠前权重越大（1/log2(rank+2)），相关才计入
        weighted_hit = 0.0
        weighted_total = 0.0
        for rank, useful in enumerate(verdicts):
            weight = 1.0 / (rank + 1)  # 简单的倒数排名加权
            weighted_total += weight
            if useful:
                weighted_hit += weight
        return weighted_hit / weighted_total if weighted_total > 0 else 0.0

    # ----- Context Recall（上下文召回率）-----

    async def context_recall(self, ground_truth: str, contexts: List[str]) -> float:
        """
        上下文召回率：ground_truth 中能被上下文覆盖的陈述比例

        Args:
            ground_truth: 标准答案（人工编写）
            contexts: 检索到的上下文片段列表

        Returns:
            0.0-1.0，越高越好
        """
        if not ground_truth or not contexts:
            return 0.0
        context = "\n\n".join(contexts)
        statements = await self._extract_statements(ground_truth)
        if not statements:
            return 0.0
        tasks = [self._verify_statement(s, context) for s in statements]
        verdicts = await asyncio.gather(*tasks, return_exceptions=False)
        covered = sum(1 for v in verdicts if v)
        return covered / len(statements)

    # ----- 综合评估 -----

    async def evaluate(
        self,
        question: str,
        answer: str,
        contexts: List[str],
        ground_truth: Optional[str] = None,
    ) -> Dict[str, float]:
        """
        对单个样本评估所有 RAGAS 指标

        Args:
            question: 用户问题
            answer: RAG 生成的答案
            contexts: 检索到的上下文片段列表
            ground_truth: 标准答案（可选，用于 context_recall）

        Returns:
            指标字典，未计算的指标不包含在内
        """
        scores: Dict[str, float] = {}

        # 并发计算 faithfulness 和 answer_relevancy 和 context_precision
        tasks = {
            "faithfulness": self.faithfulness(answer, contexts),
            "answer_relevancy": self.answer_relevancy(question, answer),
            "context_precision": self.context_precision(question, contexts),
        }
        if ground_truth:
            tasks["context_recall"] = self.context_recall(ground_truth, contexts)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning(f"RAGAS 指标 {name} 计算失败: {result}")
                scores[name] = 0.0
            else:
                scores[name] = result

        return scores


# ========== 端到端评估流程 ==========


async def run_ragas_eval(
    docs_dir: Optional[str] = None,
    queries_path: Optional[str] = None,
    persist_dir: Optional[str] = None,
    cleanup: bool = True,
    max_rag_results: int = 5,
    rag_content_limit: int = 400,
    require_ground_truth: bool = False,
) -> Dict[str, Any]:
    """
    运行 RAGAS 端到端评估

    流程：
    1. 构建评测索引（复用 build_eval_index_from_files）
    2. 对每个查询：检索 → 生成答案 → 提取 contexts → RAGAS 评分

    Returns:
        {
            "summary": {metric: mean_score},
            "per_query": [{question, answer, contexts, scores}],
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

        evaluator = RAGASEvaluator(
            llm=llm,
            embedding_model=store.embedding_model,
        )

        per_query_scores: List[Dict[str, float]] = []
        raw_results: List[Dict[str, Any]] = []

        for case in queries:
            question = case["query"]
            ground_truth = case.get("ground_truth")
            if require_ground_truth and not ground_truth:
                continue

            # 1. 检索获取 contexts（分开的片段列表）
            search_results = memory.search_knowledge(question, max_rag_results)
            contexts = [r.get("content", "") for r in search_results if r.get("content")]
            if rag_content_limit and rag_content_limit > 0:
                contexts = [c[:rag_content_limit] for c in contexts]

            # 2. 生成答案
            answer = await _generate_answer(
                memory=memory,
                llm=llm,
                query=question,
                max_rag_results=max_rag_results,
                rag_content_limit=rag_content_limit,
            )

            # 3. RAGAS 评分
            scores = await evaluator.evaluate(
                question=question,
                answer=answer,
                contexts=contexts,
                ground_truth=ground_truth,
            )
            per_query_scores.append(scores)
            raw_results.append({
                "question": question,
                "answer": answer,
                "contexts": contexts,
                "ground_truth": ground_truth,
                "scores": scores,
            })
            logger.info(
                f"RAGAS 评估完成: {question[:30]}... "
                f"faithfulness={scores.get('faithfulness', 0):.3f} "
                f"relevancy={scores.get('answer_relevancy', 0):.3f} "
                f"ctx_precision={scores.get('context_precision', 0):.3f}"
            )

        # 汇总
        all_metrics = set()
        for s in per_query_scores:
            all_metrics.update(s.keys())
        summary = {
            metric: statistics.mean(
                s[metric] for s in per_query_scores if metric in s
            )
            for metric in all_metrics
        }

        return {"summary": summary, "per_query": raw_results}
    finally:
        if cleanup:
            shutil.rmtree(persist_dir, ignore_errors=True)


def print_results(results: Dict[str, Any]) -> None:
    """打印 RAGAS 评估结果"""
    summary = results["summary"]
    per_query = results["per_query"]

    print("\n" + "=" * 80)
    print("RAGAS 评估结果")
    print("=" * 80)

    print("\n汇总指标：")
    for metric, value in summary.items():
        print(f"  {metric:<25}: {value:.3f}")

    print("\n各查询详细结果：")
    for item in per_query:
        q = item["question"]
        scores = item["scores"]
        score_str = ", ".join(f"{k}={v:.2f}" for k, v in scores.items())
        print(f"  {q[:40]}: {score_str}")


async def main():
    parser = argparse.ArgumentParser(description="RAGAS 风格 RAG 评估")
    parser.add_argument("--docs-dir", type=str, default=None, help="评测文档目录")
    parser.add_argument("--queries", type=str, default=None, help="评测查询 JSON 文件")
    parser.add_argument("--max-rag-results", type=int, default=5, help="检索 top-k")
    parser.add_argument(
        "--rag-content-limit",
        type=int,
        default=400,
        help="上下文单条内容截断长度",
    )
    parser.add_argument(
        "--require-ground-truth",
        action="store_true",
        help="只评估带 ground_truth 的查询（用于 context_recall）",
    )
    args = parser.parse_args()

    results = await run_ragas_eval(
        docs_dir=args.docs_dir,
        queries_path=args.queries,
        max_rag_results=args.max_rag_results,
        rag_content_limit=args.rag_content_limit,
        require_ground_truth=args.require_ground_truth,
    )
    print_results(results)


if __name__ == "__main__":
    asyncio.run(main())
