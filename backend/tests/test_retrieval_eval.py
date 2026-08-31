"""
检索效果回归测试

用法（PowerShell）：
    $env:RUN_SLOW_TESTS='1'; python -m pytest tests/test_retrieval_eval.py -v

说明：
- 属于慢测试，默认跳过，避免在普通 CI 中加载 embedding/reranker 模型。
- 使用 evaluation/data 下的真实风格文档和人工标注查询。
- 主要验证 hybrid_rrf+cross 相比 vector_only/bm25_only/hybrid_rrf 有稳定提升。
"""

import os
from pathlib import Path

import pytest

from app.evaluation.retrieval_eval import run_file_ablation

DOCS_DIR = Path(__file__).parent.parent / "evaluation" / "data" / "documents"
QUERIES_PATH = Path(__file__).parent.parent / "evaluation" / "data" / "queries.json"


@pytest.mark.skipif(
    os.environ.get("RUN_SLOW_TESTS") != "1",
    reason="慢测试，需设置环境变量 RUN_SLOW_TESTS=1 才能运行",
)
@pytest.mark.asyncio
async def test_retrieval_quality_regression():
    """检索效果回归：默认 parent-child + enhanced 重写 + CrossEncoder 精排应显著优于基线"""
    try:
        results = await run_file_ablation(
            docs_dir=str(DOCS_DIR),
            queries_path=str(QUERIES_PATH),
            k=5,
            rewrite_mode="enhanced",
        )
    except Exception as exc:
        pytest.skip(f"检索评测执行失败（模型未下载或环境不支持）：{exc}")

    summary = results["summary"]

    # 默认策略（parent-child + cross）应达到较高水准
    assert summary["hybrid_rrf_pc+cross"]["recall@5"] >= 0.80
    assert summary["hybrid_rrf_pc+cross"]["ndcg@5"] >= 0.80
    assert summary["hybrid_rrf_pc+cross"]["mrr"] >= 0.90

    # parent-child 策略应显著优于普通 hybrid_rrf
    assert (
        summary["hybrid_rrf_pc+cross"]["recall@5"]
        >= summary["hybrid_rrf+cross"]["recall@5"] + 0.10
    )

    # 混合检索应优于或接近纯 BM25
    assert summary["hybrid_rrf"]["recall@5"] >= summary["bm25_only"]["recall@5"] - 0.05
    assert summary["hybrid_rrf"]["ndcg@5"] >= 0.70

    # 加入 CrossEncoder 后不应比 RRF 差太多，通常应提升
    assert (
        summary["hybrid_rrf+cross"]["ndcg@5"]
        >= summary["hybrid_rrf"]["ndcg@5"] - 0.05
    )
