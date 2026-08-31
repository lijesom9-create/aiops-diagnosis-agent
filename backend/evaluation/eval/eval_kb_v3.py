"""
queries_v3 评测数据集按类别评测脚本（多来源知识库 / 250+ 条 / 8 类）

用法：
    cd backend
    python evaluation/eval/eval_kb_v3.py [--top-k 8] [--categories normal,long_tail]

数据集：evaluation/data/queries_v3_*.json（普通/长尾/口语化/多跳/多模态/跨文档/多轮/负向）
指标：
- 通用检索：Recall@K、MRR、NDCG@K（基于 expected_keywords 命中）
- 多模态：图片块召回率（image_recall，命中带图片语义的 chunk）
- 负向：无关率（irrelevance rate，检索结果与查询低相关比例）
- 多轮：raw（未消解）vs rewritten（上下文改写近似）对比，验证指代消解价值

输出：evaluation/results/kb_v3_eval_report.json + 控制台摘要
依赖：复用已入库的 Qdrant 数据（同 eval_current_kb.py），不重新入库。
"""
import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.knowledge.unified_store import UnifiedKnowledgeStore
from app.retrieval.embeddings import create_embedding_model
from app.retrieval.reranker import CrossEncoderReranker

DATA_DIR = ROOT / "evaluation" / "data"
RESULTS_DIR = ROOT / "evaluation" / "results"

# 图片块特征：metadata 中标记图片的字段，或正文中的图片描述特征词
IMAGE_META_KEYS = ("img_src", "image", "image_path", "img", "picture", "type")
IMAGE_HINT_WORDS = ("架构图", "流程图", "时序图", "示意图", "拓扑图", "截图", "dashboard",
                    "面板", "图表", "曲线", "大屏", "监控图", "界面")


# ========== 指标计算 ==========

def recall_at_k(contents: List[str], keywords: List[str], k: int) -> float:
    if not keywords:
        return 0.0
    hits = set()
    for c in contents[:k]:
        hits |= {kw for kw in keywords if kw.lower() in (c or "").lower()}
    return len(hits) / len(keywords)


def mrr(contents: List[str], keywords: List[str]) -> float:
    for i, c in enumerate(contents, 1):
        if any(kw.lower() in (c or "").lower() for kw in keywords):
            return 1.0 / i
    return 0.0


def ndcg_at_k(contents: List[str], keywords: List[str], k: int) -> float:
    if not keywords:
        return 0.0
    gains, seen = [], set()
    for c in contents[:k]:
        s = {kw for kw in keywords if kw.lower() in (c or "").lower() and kw not in seen}
        seen |= s
        gains.append(len(s) / len(keywords))
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(keywords), k)))
    return dcg / idcg if idcg else 0.0


def is_image_chunk(result: Dict[str, Any]) -> bool:
    """判断检索结果是否为图片语义块（metadata 标记或内容含图片描述特征）"""
    meta = result.get("metadata") or {}
    if any(key in meta for key in IMAGE_META_KEYS):
        return True
    content = result.get("content") or ""
    # metadata 中 doc_type/title 含图片描述特征也算
    title = str(meta.get("title", "")) + " " + str(meta.get("doc_type", ""))
    return any(w in content or w in title for w in IMAGE_HINT_WORDS)


# ========== 数据集加载 ==========

def load_datasets() -> Dict[str, List[Dict]]:
    """加载 queries_v3 系列数据集，返回 {category: [query_items]}"""
    manifest_path = DATA_DIR / "queries_v3_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest 不存在: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    datasets: Dict[str, List[Dict]] = {}
    for f_info in manifest["files"]:
        fpath = DATA_DIR / f_info["file"]
        if not fpath.exists():
            logger.warning(f"数据集文件缺失，跳过: {fpath}")
            continue
        data = json.loads(fpath.read_text(encoding="utf-8"))
        category = f_info["category"]
        if category == "multi_turn":
            # 多轮组：扁平化为 turn 列表，保留 group 信息
            turns: List[Dict] = []
            for g in data["groups"]:
                for idx, t in enumerate(g["turns"]):
                    item = dict(t)
                    item.update({
                        "category": "multi_turn",
                        "group_id": g["group_id"],
                        "turn_idx": idx,
                        "history_queries": [x["query"] for x in g["turns"][:idx]],
                        "rewritten_query": ("；".join([x["query"] for x in g["turns"][:idx]])
                                            + "；" + t["query"]) if idx > 0 else t["query"],
                    })
                    turns.append(item)
            datasets["multi_turn"] = turns
        else:
            items = []
            for q in data["queries"]:
                item = dict(q)
                item["category"] = category
                items.append(item)
            datasets[category] = items
    return datasets


# ========== 评估器 ==========

class KBV3Evaluator:
    def __init__(self, top_k: int = 8, rewrite_mode: str = "enhanced"):
        self.top_k = top_k
        self.rewrite_mode = rewrite_mode
        self.store = self._init_store()

    def _init_store(self) -> UnifiedKnowledgeStore:
        logger.info("正在初始化知识库...")
        embedding_model = create_embedding_model(local_model_name="BAAI/bge-m3")
        reranker = None
        if getattr(settings, "RERANKER_ENABLED", True):
            try:
                reranker = CrossEncoderReranker(model_name="BAAI/bge-reranker-base")
                reranker._load_model()
            except Exception as e:
                logger.warning(f"Reranker 加载失败，将禁用: {e}")
        store = UnifiedKnowledgeStore(
            embedding_model=embedding_model,
            reranker=reranker,
            separate_parent_child=settings.RAG_SEPARATE_PARENT_CHILD,
            vector_store_backend=settings.VECTOR_STORE_BACKEND,
            sparse_embedding_model=embedding_model,  # BGE-M3 同源 sparse
        )
        logger.info(f"知识库初始化完成: {store.size()} 条记录")
        return store

    def _retrieve(self, query: str, chat_history: Optional[List[Dict]] = None) -> List[Dict]:
        start = time.perf_counter()
        kwargs: Dict[str, Any] = dict(
            query=query, top_k=self.top_k, rewrite_mode=self.rewrite_mode,
        )
        if chat_history:
            kwargs["chat_history"] = chat_history
        results = self.store.hybrid_search_parent_child(**kwargs)
        latency = (time.perf_counter() - start) * 1000
        for r in results:
            r["_latency_ms"] = latency
        return results

    def evaluate_standard(self, items: List[Dict], disable_conversation: bool = False) -> Dict[str, Any]:
        """通用检索指标（Recall@K / MRR / NDCG@K），支持多轮 raw/rewritten 对比"""
        per_query, scores = [], []
        for item in items:
            # conversation 模式：multi_turn 用原始 query + chat_history（让 ConversationQueryRewriter 消解指代）
            # 其他模式：multi_turn 用 rewritten_query（历史+当前拼接近似改写）
            chat_history = None
            if self.rewrite_mode == "conversation" and item.get("turn_idx") and not disable_conversation:
                query = item["query"]
                chat_history = []
                for hq in item.get("history_queries", []):
                    chat_history.append({"role": "user", "content": hq})
                    chat_history.append({"role": "assistant", "content": "(上一轮已回答)"})
            else:
                query = item.get("rewritten_query") or item["query"]
            results = self._retrieve(query, chat_history=chat_history)
            contents = [r["content"] for r in results]
            keywords = item.get("expected_keywords") or []
            rec = recall_at_k(contents, keywords, self.top_k)
            m = mrr(contents, keywords)
            n = ndcg_at_k(contents, keywords, self.top_k)
            scores.append((rec, m, n))
            per_query.append({
                "query": item["query"],
                "category": item["category"],
                "group_id": item.get("group_id"),
                "turn_idx": item.get("turn_idx"),
                "rewritten": bool(item.get("rewritten_query") and item.get("turn_idx")),
                "recall": round(rec, 3),
                "mrr": round(m, 3),
                "ndcg": round(n, 3),
                "latency_ms": round(statistics.mean([r.get("_latency_ms", 0) for r in results]), 1),
            })
        return {
            "overall": {
                "mean_recall": round(statistics.mean([s[0] for s in scores]), 3),
                "mean_mrr": round(statistics.mean([s[1] for s in scores]), 3),
                "mean_ndcg": round(statistics.mean([s[2] for s in scores]), 3),
                "count": len(scores),
            },
            "per_query": per_query,
        }

    def evaluate_multimodal(self, items: List[Dict]) -> Dict[str, Any]:
        """多模态：检查是否召回图片语义块"""
        per_query, hit = [], 0
        for item in items:
            results = self._retrieve(item["query"])
            image_hits = [r for r in results if is_image_chunk(r)]
            ok = len(image_hits) > 0
            hit += int(ok)
            per_query.append({
                "query": item["query"],
                "image_recall_hit": ok,
                "image_chunk_count": len(image_hits),
                "top_image_title": (image_hits[0].get("title") or "")[:40] if image_hits else "",
            })
        return {
            "overall": {
                "image_recall": round(hit / len(items), 3) if items else 0.0,
                "count": len(items),
            },
            "per_query": per_query,
        }

    def evaluate_negative(self, items: List[Dict]) -> Dict[str, Any]:
        """负向：检索结果应与查询低相关（max_score 低于阈值 / 无关键词命中）"""
        per_query, correct = [], 0
        for item in items:
            results = self._retrieve(item["query"])
            max_score = max((r.get("score") or 0 for r in results), default=0.0)
            # 负向查询期望：最高分低于阈值（0.35），即知识库没有相关内容
            irrelevant = max_score < 0.35
            correct += int(irrelevant)
            per_query.append({
                "query": item["query"],
                "max_score": round(max_score, 3),
                "irrelevant": irrelevant,
                "top_result_title": (results[0].get("title") or "")[:40] if results else "",
            })
        return {
            "overall": {
                "irrelevance_rate": round(correct / len(items), 3) if items else 0.0,
                "count": len(items),
            },
            "per_query": per_query,
        }


async def main():
    parser = argparse.ArgumentParser(description="queries_v3 按类别评测")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--categories", type=str, default="",
                        help="逗号分隔，如 normal,long_tail；空 = 全部")
    parser.add_argument("--skip-negative", action="store_true", help="跳过负向（可选）")
    parser.add_argument("--rewrite-mode", type=str, default="enhanced",
                        help="查询重写模式: basic | enhanced | llm | enhanced_llm | conversation")
    args = parser.parse_args()

    datasets = load_datasets()
    if args.categories:
        cats = [c.strip() for c in args.categories.split(",") if c.strip()]
        datasets = {k: v for k, v in datasets.items() if k in cats}

    evaluator = KBV3Evaluator(top_k=args.top_k, rewrite_mode=args.rewrite_mode)
    report: Dict[str, Any] = {
        "dataset": "queries_v3",
        "top_k": args.top_k,
        "rewrite_mode": args.rewrite_mode,
        "by_category": {},
        "summary": {},
    }

    category_labels = {
        "normal": "普通", "long_tail": "长尾", "colloquial": "口语化",
        "multi_hop": "多跳", "multimodal": "多模态", "cross_doc": "跨文档",
        "multi_turn": "多轮", "negative": "负向",
    }

    for cat, items in datasets.items():
        label = category_labels.get(cat, cat)
        if cat == "multimodal":
            res = evaluator.evaluate_multimodal(items)
        elif cat == "negative":
            if args.skip_negative:
                continue
            res = evaluator.evaluate_negative(items)
        else:
            res = evaluator.evaluate_standard(items)
            # 多轮附加 raw vs rewritten 对比（衡量指代消解价值）
            if cat == "multi_turn":
                raw_items = [{**i, "rewritten_query": i["query"]} for i in items]
                raw_res = evaluator.evaluate_standard(raw_items, disable_conversation=True)
                report["by_category"]["multi_turn_raw"] = raw_res
                if args.rewrite_mode == "conversation":
                    note = "conversation（LLM 指代消解 + enhanced 规则重写）"
                else:
                    note = "rewritten（历史+当前查询拼接近似改写）"
                report["by_category"]["multi_turn"] = {**res, "note": note}
                continue
        report["by_category"][cat] = res
        logger.info(f"[{label}] 完成: {res['overall']}")

    # 汇总（排除 raw 对照项）
    cats = [k for k in report["by_category"] if k != "multi_turn_raw"]
    summary: Dict[str, Any] = {}
    for cat in cats:
        summary[category_labels.get(cat, cat)] = report["by_category"][cat]["overall"]
    report["summary"] = summary

    # 输出报告
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    # 1) 最新快照（按 rewrite_mode 命名，会被下次同模式覆盖，便于程序读取最新结果）
    out = RESULTS_DIR / f"kb_v3_eval_report_{args.rewrite_mode}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"评测报告已保存: {out}")

    # 2) 带时间戳的独立副本（每次跑都保留，不覆盖，便于历史追溯）
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    history_dir = RESULTS_DIR / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"kb_v3_eval_{args.rewrite_mode}_{timestamp}.json"
    history_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"历史记录已保存: {history_file}")

    # 3) 追加一条轻量摘要到 eval_history.jsonl（一行一条，便于跨次对比）
    history_entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rewrite_mode": args.rewrite_mode,
        "top_k": args.top_k,
        "categories": args.categories or "all",
        "summary": summary,
        "report_file": str(out.name),
        "history_file": str(history_file.relative_to(RESULTS_DIR)),
    }
    history_log = RESULTS_DIR / "eval_history.jsonl"
    with open(history_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(history_entry, ensure_ascii=False) + "\n")
    logger.info(f"历史摘要已追加: {history_log}")

    # 控制台摘要
    print("\n" + "=" * 64)
    print(f"queries_v3 按类别评测结果摘要 (rewrite_mode={args.rewrite_mode})")
    print("=" * 64)
    for cat, res in report["by_category"].items():
        if cat == "multi_turn_raw":
            print(f"多轮(raw 未消解) : {res['overall']}")
            continue
        label = category_labels.get(cat, cat)
        print(f"{label:8s}: {res['overall']}")
    print("-" * 64)
    print(f"总查询实例: {sum(len(v) for k, v in datasets.items() if k in report['by_category'])}")
    print(f"历史记录: {history_file.name}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
