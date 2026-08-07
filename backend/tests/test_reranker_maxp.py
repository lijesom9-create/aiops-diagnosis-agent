"""
CrossEncoderReranker MaxP 切块聚合单元测试（mock 模型，不加载真实模型）

验证点：
1. 短文本走单块路径，行为与旧版等价
2. 长文本切块覆盖全文（首尾都在）
3. 块数封顶时 stride 自适应，仍覆盖全文
4. MaxP 聚合：文档分数 = 最高块分；Mean 聚合 = 平均
5. 长文本相关信息在文档后段时，MaxP 能正确识别（旧截断逻辑会失败）
6. 缓存命中：同 (query, chunk) 第二次调用不触发推理
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.retrieval.base import RetrievalResult
from app.retrieval.reranker import CrossEncoderReranker


class MockCrossEncoderReranker(CrossEncoderReranker):
    """mock 掉模型加载与推理：score = query 与 doc 的共享 token 比例 * 10 - 3"""
    def _load_model(self):
        self._model = object()
        return True

    def _predict_pairs(self, pairs):
        out = []
        for q, d in pairs:
            q_tokens = set(q)
            d_tokens = set(d)
            shared = len(q_tokens & d_tokens) / max(1, len(q_tokens))
            out.append(shared * 10.0 - 3.0)  # logit，sigmoid 后约 0.05 ~ 0.999
        return out


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def make_reranker(**kw):
    kw.setdefault("maxp_chunk_size", 200)
    kw.setdefault("maxp_chunk_overlap", 50)
    kw.setdefault("max_chunks_per_doc", 16)
    return MockCrossEncoderReranker(model_name="mock", enable_cache=True, **kw)


def test_split_short_single_chunk():
    rk = make_reranker()
    assert rk._split_chunks("短文本") == ["短文本"]
    print("PASS 1: 短文本单块")


def test_split_long_covers_full():
    rk = make_reranker()
    text = "运维手册内容" * 400  # 2400 字
    chunks = rk._split_chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
    # 首尾覆盖
    assert text[:150] in chunks[0] or text[:200] in chunks[0]
    assert text[-150:] in chunks[-1]
    # 无空块
    assert all(c for c in chunks)
    print(f"PASS 2: 长文本切块 {len(chunks)} 块, 首尾覆盖")


def test_split_chunk_cap():
    rk = make_reranker(max_chunks_per_doc=4)
    text = "A" * 5000
    chunks = rk._split_chunks(text)
    assert len(chunks) <= 4
    assert text[-100:] in chunks[-1], "封顶后仍须覆盖尾部"
    print(f"PASS 3: 块数封顶 -> {len(chunks)} 块, 尾部覆盖")


def test_maxp_aggregation():
    """长文档：query 只命中文档后段的一块，MaxP 应拿到该块的高分"""
    rk = make_reranker()
    query = "Redis 缓存雪崩怎么处理"
    doc = ("本文档介绍系统架构和通用部署流程。" * 30 +  # 前段无关
           "缓存雪崩怎么处理 缓存雪崩解决方案：加锁、限流、多级缓存、随机过期时间。" * 20)  # 后段相关
    result = RetrievalResult(content=doc, source="manual", score=0.5, doc_id="d1")
    ranked = rk.rerank(query, [result], limit=1)
    # 后段相关块应产生高 logit -> sigmoid 高分；无关块低分
    expected_min = sigmoid(10.0 * 0.6 - 3.0)  # 后段共享 60%+ token
    assert ranked[0].score > expected_min, f"MaxP 分数过低: {ranked[0].score:.3f}"
    # 与旧截断逻辑对比：旧逻辑只看前 256 字符（全无关），分数应远低于 MaxP
    legacy_score = sigmoid(10.0 * (len(set(query) & set(doc[:256])) / len(set(query))) - 3.0)
    assert ranked[0].score > legacy_score + 0.1, \
        f"MaxP ({ranked[0].score:.3f}) 应显著优于旧截断 ({legacy_score:.3f})"
    print(f"PASS 4: MaxP 聚合 score={ranked[0].score:.3f} > 旧截断 score={legacy_score:.3f}")


def test_mean_vs_maxp():
    """mean 聚合应低于 max（无相关块拉高），验证聚合策略生效"""
    rk_max = make_reranker(maxp_aggregation="max")
    rk_mean = make_reranker(maxp_aggregation="mean")
    query = "Kafka 消息堆积"
    doc = ("无关内容甲" * 60 + "Kafka 消费者组出现消息堆积，处理方案：扩容分区、提升消费者并发、检查消费速率。" * 15)
    r1 = RetrievalResult(content=doc, source="manual", score=0.5, doc_id="d1")
    r2 = RetrievalResult(content=doc, source="manual", score=0.5, doc_id="d1")
    s_max = rk_max.rerank(query, [r1], limit=1)[0].score
    s_mean = rk_mean.rerank(query, [r2], limit=1)[0].score
    assert s_max > s_mean, f"MaxP {s_max:.3f} 应 > Mean {s_mean:.3f}"
    print(f"PASS 5: MaxP={s_max:.3f} > Mean={s_mean:.3f}")


def test_short_doc_equivalent():
    """短文档：旧版截断=全文（未超长），行为应一致（单次推理）"""
    rk = make_reranker()
    query = "磁盘告警"
    doc = "磁盘使用率超过 85% 触发告警，处理流程见响应手册。"
    r1 = RetrievalResult(content=doc, source="manual", score=0.5, doc_id="d1")
    r2 = RetrievalResult(content=doc, source="manual", score=0.5, doc_id="d1")
    s1 = rk.rerank(query, [r1], limit=1)[0].score
    s2 = rk.rerank(query, [r2], limit=1)[0].score
    assert s1 == s2
    print(f"PASS 6: 短文档重复调用分数一致 {s1:.3f}")


def test_cache_hit():
    rk = make_reranker()
    query = "Nginx 502"
    doc = "Nginx 502 网关错误的排查步骤：检查上游服务、查看 error.log、验证端口连通性。" * 3
    r1 = RetrievalResult(content=doc, source="manual", score=0.5, doc_id="d1")
    hits0 = rk._cache_hits
    rk.rerank(query, [r1], limit=1)
    misses_first = rk._cache_misses
    rk.rerank(query, [r1], limit=1)
    assert rk._cache_hits > hits0, "第二次调用应命中缓存"
    assert rk._cache_misses == misses_first, "第二次调用不应新增推理"
    print(f"PASS 7: 缓存命中 (hits={rk._cache_hits}, misses={rk._cache_misses})")


def test_ranking_with_long_docs():
    """多个长文档排序：相关文档（后段命中）应排在最前"""
    rk = make_reranker()
    query = "MySQL 主从延迟"
    relevant = ("系统概述与架构设计。数据库运维基础知识。" * 40 +
                "MySQL 主从延迟产生的原因：大事务、慢查询、主库写压力大。解决：并行复制、限流、拆事务。" * 20)
    irrelevant = "前端页面开发规范与组件库使用说明。" * 120
    r_rel = RetrievalResult(content=relevant, source="manual", score=0.4, doc_id="rel")
    r_irr = RetrievalResult(content=irrelevant, source="manual", score=0.6, doc_id="irr")
    ranked = rk.rerank(query, [r_irr, r_rel], limit=2)
    assert ranked[0].doc_id == "rel", f"相关文档应排第一，实际: {[r.doc_id for r in ranked]}"
    print(f"PASS 8: 长文档重排 {[r.doc_id for r in ranked]}")


if __name__ == "__main__":
    test_split_short_single_chunk()
    test_split_long_covers_full()
    test_split_chunk_cap()
    test_maxp_aggregation()
    test_mean_vs_maxp()
    test_short_doc_equivalent()
    test_cache_hit()
    test_ranking_with_long_docs()
    print("\n全部单元测试通过 ✔")
