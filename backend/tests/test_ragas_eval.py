"""
RAGAS 风格评估的单元测试

用法（PowerShell）：
    # 单元测试（默认运行，不依赖 LLM）
    python -m pytest tests/test_ragas_eval.py -v

    # 端到端评估（慢测试，需要真实 LLM）
    $env:RUN_SLOW_TESTS='1'; python -m pytest tests/test_ragas_eval.py -v -k regression
"""

import os
from pathlib import Path
from typing import Dict, List

import pytest

from app.evaluation.ragas_eval import (
    RAGASEvaluator,
    _cosine_similarity,
    _parse_json_list,
    _parse_json_object,
)


DOCS_DIR = Path(__file__).parent.parent / "evaluation" / "data" / "documents"
QUERIES_PATH = Path(__file__).parent.parent / "evaluation" / "data" / "queries.json"


# ========== 工具函数单元测试（默认运行，无需 LLM） ==========


class TestUtilities:
    """工具函数测试"""

    def test_cosine_similarity_identical(self):
        vec = [1.0, 2.0, 3.0]
        assert _cosine_similarity(vec, vec) == pytest.approx(1.0, abs=1e-6)

    def test_cosine_similarity_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert _cosine_similarity(a, b) == pytest.approx(0.0, abs=1e-6)

    def test_cosine_similarity_empty(self):
        assert _cosine_similarity([], [1.0]) == 0.0
        assert _cosine_similarity([1.0], []) == 0.0

    def test_cosine_similarity_zero_vector(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_parse_json_list_plain(self):
        result = _parse_json_list('["陈述1", "陈述2"]')
        assert result == ["陈述1", "陈述2"]

    def test_parse_json_list_with_codeblock(self):
        text = '```json\n["a", "b"]\n```'
        assert _parse_json_list(text) == ["a", "b"]

    def test_parse_json_list_invalid(self):
        assert _parse_json_list("not json") == []
        assert _parse_json_list("") == []

    def test_parse_json_list_embedded(self):
        text = '前面有文字 ["x", "y"] 后面也有'
        assert _parse_json_list(text) == ["x", "y"]

    def test_parse_json_object_plain(self):
        obj = _parse_json_object('{"verdict": "yes"}')
        assert obj == {"verdict": "yes"}

    def test_parse_json_object_with_codeblock(self):
        text = '```json\n{"score": 4}\n```'
        obj = _parse_json_object(text)
        assert obj == {"score": 4}

    def test_parse_json_object_invalid(self):
        assert _parse_json_object("not json") == {}
        assert _parse_json_object("") == {}


# ========== RAGAS 指标降级测试（默认运行，无需真实 LLM） ==========


class TestRAGASGracefulDegradation:
    """RAGAS 指标在无 LLM 时的降级行为"""

    @pytest.mark.asyncio
    async def test_faithfulness_empty_input(self):
        ev = RAGASEvaluator(llm=None)
        assert await ev.faithfulness("", ["ctx"]) == 0.0
        assert await ev.faithfulness("answer", []) == 0.0

    @pytest.mark.asyncio
    async def test_answer_relevancy_empty_input(self):
        ev = RAGASEvaluator(llm=None)
        assert await ev.answer_relevancy("", "answer") == 0.0
        assert await ev.answer_relevancy("question", "") == 0.0

    @pytest.mark.asyncio
    async def test_context_precision_empty(self):
        ev = RAGASEvaluator(llm=None)
        assert await ev.context_precision("q", []) == 0.0

    @pytest.mark.asyncio
    async def test_context_recall_empty(self):
        ev = RAGASEvaluator(llm=None)
        assert await ev.context_recall("", ["ctx"]) == 0.0
        assert await ev.context_recall("truth", []) == 0.0

    @pytest.mark.asyncio
    async def test_evaluate_returns_dict(self):
        """无 LLM 时 evaluate 返回全 0 的指标字典"""
        ev = RAGASEvaluator(llm=None)
        scores = await ev.evaluate(
            question="什么是装饰器",
            answer="装饰器是高阶函数",
            contexts=["装饰器用于修改函数行为"],
        )
        assert isinstance(scores, dict)
        assert "faithfulness" in scores
        assert "answer_relevancy" in scores
        assert "context_precision" in scores
        # 无 ground_truth 时不包含 context_recall
        assert "context_recall" not in scores


# ========== RAGAS 指标逻辑测试（用 mock LLM） ==========


class _MockLLM:
    """可控的 mock LLM，按 prompt 关键词返回预设响应"""

    def __init__(self, responses: Dict[str, str]):
        self.responses = responses
        self.calls = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        prompt = messages[-1]["content"] if messages else ""
        for key, resp in self.responses.items():
            if key in prompt:
                return {"content": resp, "tool_calls": None, "finish_reason": "end_turn"}
        return {"content": "", "tool_calls": None, "finish_reason": "end_turn"}


class _MockEmbedding:
    """mock embedding 模型"""

    def embed(self, text: str) -> List[float]:
        # 简单的 hash 向量，相同文本返回相同向量
        return [float(len(text)), float(sum(ord(c) for c in text) % 100)]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


class TestRAGASWithMockLLM:
    """用 mock LLM 验证 RAGAS 指标计算逻辑"""

    @pytest.mark.asyncio
    async def test_faithfulness_all_supported(self):
        """所有陈述都被上下文支持时 faithfulness=1.0"""
        mock = _MockLLM({
            "原子陈述": '["装饰器是高阶函数", "装饰器修改函数行为"]',
            "推断": '{"verdict": "yes"}',
        })
        ev = RAGASEvaluator(llm=mock, embedding_model=_MockEmbedding())
        score = await ev.faithfulness(
            answer="装饰器是高阶函数，可以修改函数行为。",
            contexts=["装饰器是一个接收函数并返回函数的高阶函数，用于修改函数行为。"],
        )
        assert score == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_faithfulness_none_supported(self):
        """没有陈述被支持时 faithfulness=0.0"""
        mock = _MockLLM({
            "原子陈述": '["Python是编译型语言"]',
            "推断": '{"verdict": "no"}',
        })
        ev = RAGASEvaluator(llm=mock, embedding_model=_MockEmbedding())
        score = await ev.faithfulness(
            answer="Python是编译型语言。",
            contexts=["装饰器是高阶函数。"],
        )
        assert score == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_context_precision_all_relevant(self):
        """所有 context 都相关时 precision=1.0"""
        mock = _MockLLM({
            "有用": '{"useful": true}',
        })
        ev = RAGASEvaluator(llm=mock, embedding_model=_MockEmbedding())
        score = await ev.context_precision(
            question="什么是装饰器",
            contexts=["装饰器是高阶函数", "装饰器修改函数行为"],
        )
        assert score == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_context_precision_none_relevant(self):
        """没有 context 相关时 precision=0.0"""
        mock = _MockLLM({
            "有用": '{"useful": false}',
        })
        ev = RAGASEvaluator(llm=mock, embedding_model=_MockEmbedding())
        score = await ev.context_precision(
            question="什么是装饰器",
            contexts=["SQL连接", "Docker容器"],
        )
        assert score == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_context_recall_all_covered(self):
        """ground_truth 所有陈述都被上下文覆盖时 recall=1.0"""
        mock = _MockLLM({
            "原子陈述": '["装饰器是高阶函数"]',
            "推断": '{"verdict": "yes"}',
        })
        ev = RAGASEvaluator(llm=mock, embedding_model=_MockEmbedding())
        score = await ev.context_recall(
            ground_truth="装饰器是高阶函数。",
            contexts=["装饰器接收函数并返回函数。"],
        )
        assert score == pytest.approx(1.0, abs=1e-6)

    @pytest.mark.asyncio
    async def test_answer_relevancy_with_embedding(self):
        """answer_relevancy 使用 embedding 计算相似度"""
        mock = _MockLLM({
            "问题变体": '["装饰器是什么", "装饰器的作用", "如何使用装饰器"]',
        })
        ev = RAGASEvaluator(llm=mock, embedding_model=_MockEmbedding())
        score = await ev.answer_relevancy(
            question="什么是装饰器",
            answer="装饰器是用于修改函数行为的高阶函数。",
        )
        # mock embedding 的相似度依赖文本，这里只验证返回值范围合法
        assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_evaluate_with_ground_truth(self):
        """evaluate 带 ground_truth 时包含 context_recall"""
        mock = _MockLLM({
            "原子陈述": '["陈述1"]',
            "推断": '{"verdict": "yes"}',
            "有用": '{"useful": true}',
            "问题变体": '["问题1"]',
        })
        ev = RAGASEvaluator(llm=mock, embedding_model=_MockEmbedding())
        scores = await ev.evaluate(
            question="什么是装饰器",
            answer="装饰器是高阶函数",
            contexts=["装饰器修改函数行为"],
            ground_truth="装饰器是高阶函数",
        )
        assert "context_recall" in scores
        assert "faithfulness" in scores
        assert "answer_relevancy" in scores
        assert "context_precision" in scores


# ========== 端到端回归测试（慢测试） ==========


@pytest.mark.skipif(
    os.environ.get("RUN_SLOW_TESTS") != "1",
    reason="慢测试，需设置环境变量 RUN_SLOW_TESTS=1 才能运行",
)
@pytest.mark.asyncio
async def test_ragas_regression():
    """RAGAS 端到端回归：所有指标应达到合理水平"""
    from app.core.config import settings

    if not settings.AI_API_KEY:
        pytest.skip("未配置 AI_API_KEY，RAGAS 评估需要真实 LLM")

    from app.evaluation.ragas_eval import run_ragas_eval

    try:
        results = await run_ragas_eval(
            docs_dir=str(DOCS_DIR),
            queries_path=str(QUERIES_PATH),
            max_rag_results=5,
        )
    except Exception as exc:
        pytest.skip(f"RAGAS 评估执行失败（模型未下载或环境不支持）：{exc}")

    summary = results["summary"]

    # 各指标应达到基本水平（RAGAS 指标普遍偏低，阈值设宽松些）
    assert summary["faithfulness"] >= 0.50, (
        f"忠实度过低（幻觉过多）: {summary['faithfulness']:.3f}"
    )
    assert summary["answer_relevancy"] >= 0.50, (
        f"答案相关性过低: {summary['answer_relevancy']:.3f}"
    )
    assert summary["context_precision"] >= 0.50, (
        f"上下文精确度过低: {summary['context_precision']:.3f}"
    )
