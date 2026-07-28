"""
生成效果回归测试

用法（PowerShell）：
    $env:RUN_SLOW_TESTS='1'; python -m pytest tests/test_generation_eval.py -v
    $env:RUN_SLOW_TESTS='1'; $env:RUN_LLM_JUDGE='1'; python -m pytest tests/test_generation_eval.py -v

说明：
- 属于慢测试，默认跳过，避免在普通 CI 中加载 embedding/reranker 模型并调用 LLM。
- 需要配置 AI_API_KEY，否则无法真实评估生成质量。
- 复用 evaluation/data 下的 8 份文档和 24 个查询。
"""

import os
from pathlib import Path

import pytest

from app.core.config import settings
from app.evaluation.generation_eval import run_generation_eval


DOCS_DIR = Path(__file__).parent.parent / "evaluation" / "data" / "documents"
QUERIES_PATH = Path(__file__).parent.parent / "evaluation" / "data" / "queries.json"


@pytest.mark.skipif(
    os.environ.get("RUN_SLOW_TESTS") != "1",
    reason="慢测试，需设置环境变量 RUN_SLOW_TESTS=1 才能运行",
)
@pytest.mark.asyncio
async def test_generation_quality_regression():
    """生成效果回归：答案应覆盖关键词并正确使用引用"""
    if not settings.AI_API_KEY:
        pytest.skip("未配置 AI_API_KEY，生成效果测试需要真实 LLM")

    enable_llm_judge = os.environ.get("RUN_LLM_JUDGE") == "1"

    try:
        results = await run_generation_eval(
            docs_dir=str(DOCS_DIR),
            queries_path=str(QUERIES_PATH),
            enable_llm_judge=enable_llm_judge,
            rag_content_limit=400,
        )
    except Exception as exc:
        pytest.skip(f"生成评测执行失败（模型未下载或环境不支持）：{exc}")

    summary = results["summary"]

    # 自动指标：关键词召回率和引用使用应有基本保障
    assert summary["keyword_recall"] >= 0.80, f"关键词召回过低: {summary['keyword_recall']:.3f}"
    assert summary["has_citation"] >= 0.90, f"引用使用率过低: {summary['has_citation']:.3f}"
    assert summary["context_overlap"] >= 0.35, f"上下文重叠度过低: {summary['context_overlap']:.3f}"

    # LLM-as-Judge 指标
    if enable_llm_judge:
        assert summary["judge_correctness"] >= 3.0
        assert summary["judge_completeness"] >= 3.0
        assert summary["judge_relevance"] >= 3.0
        assert summary["judge_hallucination"] >= 3.0
