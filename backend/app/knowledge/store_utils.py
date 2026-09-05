"""UnifiedKnowledgeStore 的纯逻辑辅助函数（T3-C 从 unified_store.py 抽离，无实例状态）。

这些函数只依赖入参，不访问 self/外部状态，可独立复用与单测。
UnifiedKnowledgeStore 类保留同名薄委托方法，对外调用契约不变。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def merge_filters(
    base: Dict[str, Any], extra: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """合并两个 Qdrant 风格 filter（支持 $and 嵌套）

    用于把 metadata_filter（如 {"service":"payment-service"}）合并到基础 filter
    （如 {"chunk_type":"child"}），生成 {"$and":[...]} 传给 Qdrant。
    """
    if not extra:
        return base or {}
    if not base:
        return extra
    conditions: List[Dict[str, Any]] = []
    if "$and" in base:
        conditions.extend(base["$and"])
    else:
        conditions.append(base)
    if "$and" in extra:
        conditions.extend(extra["$and"])
    else:
        conditions.append(extra)
    return {"$and": conditions}


def match_metadata_filter(
    metadata: Dict[str, Any], meta_filter: Optional[Dict[str, Any]]
) -> bool:
    """检查 metadata 是否满足 meta_filter（Python 层过滤，给 BM25 内存索引用）

    BM25 是内存索引不支持原生 filter，检索后在 Python 层按 meta_filter 过滤。
    支持：
    - {"key": "value"} 精确匹配
    - {"$and": [...]} 全部满足
    - {"$or": [...]} 任一满足
    - {"$or_empty": {"key": k, "value": v}} 字段为空 / 等于 v（可见性过滤，字段一定存在）
    - {"$or_missing": {"key": k, "value": v}} / {"$or_null": ...} 字段不存在 / 为空 / 等于 v（可见性过滤）
    """
    if not meta_filter:
        return True
    for k, v in meta_filter.items():
        if k == "$and":
            for sub in v:
                if not match_metadata_filter(metadata, sub):
                    return False
        elif k == "$or":
            if not any(match_metadata_filter(metadata, sub) for sub in v):
                return False
        elif k in ("$or_empty", "$or_missing", "$or_null"):
            # Python 层三者语义一致：字段不存在(None) / 空串 / 等于目标值 → 可见
            key = v["key"]
            val = v["value"]
            actual = metadata.get(key)
            if actual not in (None, "", val):
                return False
        else:
            if metadata.get(k) != v:
                return False
    return True


def build_visibility_filter(
    source: Optional[str] = None,
    metadata_filter: Optional[Dict[str, Any]] = None,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """构造可见性 + 业务过滤条件（三路检索共用，保证过滤行为一致）

    合并：
    - source: 来源过滤
    - metadata_filter: 业务元数据过滤（如 service/doc_type）
    - 可见性语义：shared OR (org 条件 AND user 条件)
      shared_to_diagnosis="true" 的文档对诊断服务可见（显式共享，旁路隔离）；
      未共享的文档保持原有 AND 语义——不能把 org/user 条件彼此 OR
      （否则"别人组织下的他人私有文档"会漏出来）

    存储约定差异：
    - org_id: KnowledgeItem.to_chroma 强制写入（公共文档 org_id=""），故用 $or_empty
    - user_id: uploader._store_chunks 的 cleaned 会移除空值（公共文档无 user_id 字段），故用 $or_missing
    - shared_to_diagnosis: 字符串 "true"/"false" 显式共享标记（缺失 = 未共享）

    Returns:
        filter dict，可能为 {}（无条件）。供 Qdrant pre-filter 和 BM25 Python post-filter 共用。
    """
    f: Dict[str, Any] = {}
    if source:
        f = merge_filters(f, {"source": source})
    if metadata_filter:
        f = merge_filters(f, metadata_filter)
    visibility_conds: List[Dict[str, Any]] = []
    if org_id:
        visibility_conds.append({"$or_empty": {"key": "org_id", "value": org_id}})
    if user_id:
        visibility_conds.append({"$or_missing": {"key": "user_id", "value": user_id}})
    if visibility_conds:
        f = merge_filters(f, {"$or": [
            {"shared_to_diagnosis": "true"},
            {"$and": visibility_conds},
        ]})
    return f


def build_child_filter(
    source: Optional[str] = None,
    metadata_filter: Optional[Dict[str, Any]] = None,
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """构造子块检索 filter = chunk_type=child + 可见性过滤"""
    visibility = build_visibility_filter(source, metadata_filter, org_id, user_id)
    return merge_filters({"chunk_type": "child"}, visibility)


def rrf_fuse(
    results_a: List[Dict], results_b: List[Dict], k: int = 60,
    weight_a: float = 1.0, weight_b: float = 1.0,
) -> List[Dict]:
    """加权 RRF (Reciprocal Rank Fusion) 融合两路结果

    公式: score(d) = weight_a / (k + rank_a + 1) + weight_b / (k + rank_b + 1)
    weight_a / weight_b 控制两路的相对重要性（默认 1:1 等价标准 RRF）
    """
    scores: Dict[str, float] = {}
    doc_map: Dict[str, Dict] = {}

    for rank, doc in enumerate(results_a):
        doc_id = doc["id"]
        scores[doc_id] = scores.get(doc_id, 0) + weight_a / (k + rank + 1)
        if doc_id not in doc_map:
            doc_map[doc_id] = doc

    for rank, doc in enumerate(results_b):
        doc_id = doc["id"]
        scores[doc_id] = scores.get(doc_id, 0) + weight_b / (k + rank + 1)
        if doc_id not in doc_map:
            doc_map[doc_id] = doc

    sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    result = []
    for doc_id, score in sorted_ids:
        entry = dict(doc_map[doc_id])
        entry["score"] = score
        result.append(entry)

    return result


def tokenize(text: str) -> List[str]:
    """中英文分词"""
    if not text:
        return []
    try:
        import jieba
        english_words = re.findall(r'[a-zA-Z]+', text.lower())
        chinese_text = re.sub(r'[a-zA-Z]+', '', text.lower())
        chinese_words = list(jieba.cut(chinese_text))
        stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这"}
        return english_words + [w for w in chinese_words if len(w) > 1 and w not in stop_words]
    except ImportError:
        return re.findall(r'[\w一-鿿]+', text.lower())