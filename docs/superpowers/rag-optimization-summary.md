# RAG 检索链路调优全记录

> 统计窗口：2026-07-28 会话
> 评测指标：Recall@5、MRR、NDCG@5
> 最终默认配置：`parent_child` 分块 + `hybrid_search_parent_child` + `enhanced` 查询重写 + `CrossEncoder` 重排

---

## 一、调优时间线

| 阶段 | 优化方向 | 具体改动 | 评测集 | 核心策略 | recall@5 | mrr | ndcg@5 | 关键结论 |
|------|---------|---------|--------|---------|----------|-----|--------|---------|
| 基线 | 单路检索 | vector_only / bm25_only | 36 查询 | vector_only | 0.615 | 0.925 | 0.790 | 单路召回不足 |
| 基线 | 单路检索 | vector_only / bm25_only | 36 查询 | bm25_only | 0.424 | 0.929 | 0.637 | 纯关键词召回更差 |
| 优化 1 | 混合检索 + 重排 | 修正 BM25 IDF 为标准对数公式；接入 `BAAI/bge-reranker-base` | 24 查询 | hybrid_rrf+cross | **0.770** | **1.000** | **0.919** | RRF + CrossEncoder 显著提升 NDCG |
| 优化 2 | 父子文档分块 | 新增 `parent_child_chunker.py`；子块检索、父块返回；增加 parent 关键词过滤 | 24 查询 | hybrid_rrf_pc+cross | **0.875 ~ 0.883** | **0.940 ~ 0.972** | **0.859 ~ 0.893** | 全链路最大收益点 |
| 优化 3 | 查询重写 | `QueryRewriter` 增加技术术语中英文同义词/缩写双向扩展；`enhanced` 重写模式 | 36 查询 | hybrid_rrf_pc+cross | basic: 0.875<br>enhanced: **0.883** | basic: 0.940<br>enhanced: **0.972** | basic: 0.859<br>enhanced: **0.893** | enhanced 在 parent-child 下稳定提升 |
| 优化 4 | 扩展数据集 | 24 → 36 查询；新增 Redis、SQLAlchemy、Celery、async/await 等领域 | 36 查询 | hybrid_rrf+cross | 0.673（较 24 查询的 0.770 下降） | 1.000 | 0.873（较 24 查询的 0.919 下降） | 数据集更能反映真实难点 |
| 优化 5 | 重排器参数 | 扫描 `candidate_multiplier` = 2 / 3 / 4 | 36 查询 | hybrid_rrf+cross | 2: 0.677<br>3: 0.673<br>4: **0.683** | 均为 1.000 | 2: 0.877<br>3: 0.872<br>4: **0.879** | 边际收益小，默认保持 3 |
| 最终落地 | 默认配置切换 | Uploader 默认 `parent_child`；服务调用切到 `hybrid_search_parent_child`；默认 `enhanced` 重写 | 36 查询 | hybrid_rrf_pc+cross | **0.883** | **0.972** | **0.893** | 检索回归测试通过 |

---

## 二、关键指标对比

### 2.1 检索策略对比（36 查询集，rewrite_mode=enhanced）

| 策略 | recall@5 | mrr | ndcg@5 |
|------|----------|-----|--------|
| vector_only | 0.615 | 0.925 | 0.790 |
| bm25_only | 0.424 | 0.929 | 0.637 |
| hybrid_rrf | 0.562 | 0.958 | 0.726 |
| hybrid_rrf+cross | 0.673 | 1.000 | 0.872 |
| hybrid_rrf_pc | 0.883 | 0.972 | 0.893 |
| hybrid_rrf_pc+cross | **0.883** | **0.972** | **0.893** |

> 说明：parent-child 策略中 `+simple` / `+cross` 与未重排在该数据集上分数相同，因为 top-5 已能覆盖所有相关父块。

### 2.2 查询重写对比（36 查询集）

| 策略 | rewrite_mode | recall@5 | mrr | ndcg@5 |
|------|--------------|----------|-----|--------|
| hybrid_rrf | basic | 0.587 | 0.918 | 0.735 |
| hybrid_rrf | enhanced | 0.562 | 0.958 | 0.726 |
| hybrid_rrf+cross | basic | 0.673 | 1.000 | 0.873 |
| hybrid_rrf+cross | enhanced | 0.673 | 1.000 | 0.872 |
| hybrid_rrf_pc+cross | basic | 0.875 | 0.940 | 0.859 |
| hybrid_rrf_pc+cross | enhanced | **0.883** | **0.972** | **0.893** |

> 结论：`enhanced` 重写在 parent-child 场景下才能稳定释放价值。

### 2.3 重排器参数扫描（36 查询集，hybrid_rrf+cross，enhanced）

| candidate_multiplier | recall@5 | mrr | ndcg@5 |
|----------------------|----------|-----|--------|
| 2 | 0.677 | 1.000 | 0.877 |
| 3 | 0.673 | 1.000 | 0.872 |
| 4 | 0.683 | 1.000 | 0.879 |

### 2.4 生成效果评估

| 指标 | 充值优化前 | 充值优化后 | 变化 |
|------|-----------|-----------|------|
| keyword_recall | 0.938 | **0.958** | +0.020 |
| has_citation | - | 1.000 | - |
| citation_count | - | 8.417 | - |
| context_overlap | - | 0.673 | - |
| judge_correctness | - | **5.000** | - |
| judge_completeness | - | **4.958** | - |

---

## 三、核心经验

1. **父子文档分块是检索链路最大收益点**：recall@5 从 0.673 提升到 0.883，NDCG@5 从 0.872 提升到 0.893。
2. **查询重写需要与分块策略匹配**：普通分块下 enhanced 重写收益不明显；parent-child 下 enhanced 重写可稳定提升 recall 与 NDCG。
3. **CrossEncoder 重排对 NDCG/MRR 提升显著**：MRR 从 0.918 提升到 1.000，NDCG@5 从 0.735 提升到 0.873。
4. **扩展数据集会降低表面指标，但更有价值**：24 查询集上 hybrid_rrf+cross recall@5=0.770，36 查询集上降至 0.673，说明新增查询更能暴露真实召回短板。
5. **重排候选池边际收益小**：candidate_multiplier 从 3 调到 4 仅带来 recall@5 +0.010，默认保持 3 以平衡延迟。

---

## 四、最终服务默认配置

```python
# DocumentUploader 默认分块策略
chunking_strategy="parent_child"

# 服务检索调用
_knowledge_store.hybrid_search_parent_child(
    query, top_k=limit, rewrite_query=True
)

# UnifiedKnowledgeStore.hybrid_search_parent_child 默认参数
rewrite_mode="enhanced"
rrf_k=60
candidate_multiplier=3
```

---

## 五、相关文件

- 评测数据：`backend/evaluation/data/documents/`、`backend/evaluation/data/queries.json`
- 评测结果：`backend/evaluation/results/ablation_1_3_4.json`
- 指标表格：`backend/evaluation/results/rag_optimization_metrics.csv`
- 可视化图表：`backend/evaluation/results/rag_optimization_*.png`
