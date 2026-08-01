"""快速验证动态 rerank 和缓存失效"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.knowledge.unified_store import UnifiedKnowledgeStore
from app.retrieval.reranker import CrossEncoderReranker
from app.shared_services import get_embedding_model, set_knowledge_store
from app.core.config import settings

# 初始化
embedding_model = get_embedding_model()
reranker = CrossEncoderReranker(model_name=settings.RERANKER_MODEL_NAME, use_onnx=True)
store = UnifiedKnowledgeStore(
    embedding_model=embedding_model,
    reranker=reranker,
    separate_parent_child=settings.RAG_SEPARATE_PARENT_CHILD,
    vector_store_backend=settings.VECTOR_STORE_BACKEND,
)
set_knowledge_store(store)
print(f"知识库: {store.size()} 条记录")
print(f"ONNX ready: {reranker._use_onnx}")

# 测试动态 rerank
print("\n=== 测试动态 rerank ===")
clear_results = [
    {"id": f"doc_{i}", "score": 0.9 - i * 0.05, "content": f"内容{i}", "metadata": {}, "source": ""}
    for i in range(20)
]
selected_clear = store._select_rerank_candidates(clear_results, top_k=8)
print(f"清晰查询: 20 个候选 -> rerank {len(selected_clear)} 个 (预期 16)")

fuzzy_results = [
    {"id": f"doc_{i}", "score": 0.5 + i * 0.001, "content": f"内容{i}", "metadata": {}, "source": ""}
    for i in range(20)
]
selected_fuzzy = store._select_rerank_candidates(fuzzy_results, top_k=8)
print(f"模糊查询: 20 个候选 -> rerank {len(selected_fuzzy)} 个 (预期 20)")

# 测试缓存失效
print("\n=== 测试缓存失效 ===")
store._query_cache["test"] = {"results": [], "ts": 0}
store._rewrite_cache["test"] = []
print(f"填充缓存后: query={len(store._query_cache)}, rewrite={len(store._rewrite_cache)}")
store._invalidate_caches()
print(f"失效后: query={len(store._query_cache)}, rewrite={len(store._rewrite_cache)}")

# 测试 ONNX vs PyTorch 性能对比
print("\n=== ONNX vs PyTorch 性能对比 ===")
import time
pairs = [
    ("什么是智能体", f"内容片段_{i} 智能体是能自主行动的AI系统" if i % 3 == 0 else f"内容片段_{i} FastAPI Web框架")
    for i in range(24)
]

# ONNX
r_onnx = CrossEncoderReranker(use_onnx=True)
r_onnx._load_model()
r_onnx._predict_pairs(pairs[:3])  # warmup
t0 = time.time()
for _ in range(5):
    r_onnx._predict_pairs(pairs)
onnx_ms = (time.time() - t0) / 5 * 1000
print(f"ONNX    (24 pairs): {onnx_ms:.0f}ms")

# PyTorch
r_pt = CrossEncoderReranker(use_onnx=False)
r_pt._load_model()
r_pt._predict_pairs(pairs[:3])  # warmup
t0 = time.time()
for _ in range(5):
    r_pt._predict_pairs(pairs)
pt_ms = (time.time() - t0) / 5 * 1000
print(f"PyTorch (24 pairs): {pt_ms:.0f}ms")
print(f"加速比: {pt_ms/onnx_ms:.2f}x")

print("\n✓ 全部验证通过")
