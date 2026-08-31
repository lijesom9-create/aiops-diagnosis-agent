"""
融合算法模块

实现多种检索结果融合策略：
- RRF (Reciprocal Rank Fusion)
- Weighted Sum
- Convex Combination
- CombMNZ
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FusionResult:
    """融合结果"""
    doc_id: str
    content: str
    score: float
    source: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "doc_id": self.doc_id,
            "content": self.content[:200],
            "score": self.score,
            "source": self.source,
            "metadata": self.metadata,
        }


class RRFFusion:
    """
    Reciprocal Rank Fusion (RRF)

    公式: score(d) = sum(1 / (k + rank_i(d)))
    k 通常取 60
    """

    def __init__(self, k: int = 60):
        self.k = k

    def fuse(
        self,
        result_lists: List[List[Dict]],
        source_names: Optional[List[str]] = None,
    ) -> List[FusionResult]:
        """
        融合多路检索结果

        Args:
            result_lists: 多路检索结果，每路是 dict 列表
            source_names: 各路的来源名称

        Returns:
            List[FusionResult]: 融合后的结果
        """
        if not source_names:
            source_names = [f"source_{i}" for i in range(len(result_lists))]

        # 收集所有文档的 RRF 分数
        scores: Dict[str, float] = {}
        doc_data: Dict[str, Dict] = {}

        for results, source_name in zip(result_lists, source_names):
            for rank, doc in enumerate(results):
                doc_id = doc.get("doc_id", doc.get("id", ""))
                if not doc_id:
                    continue

                # RRF 分数
                rrf_score = 1.0 / (self.k + rank + 1)
                scores[doc_id] = scores.get(doc_id, 0.0) + rrf_score

                # 保存文档数据（以第一次出现的为准）
                if doc_id not in doc_data:
                    doc_data[doc_id] = {
                        "doc_id": doc_id,
                        "content": doc.get("content", ""),
                        "metadata": doc.get("metadata", {}),
                        "source": source_name,
                    }

        # 按分数排序
        sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        # 构建结果
        results = []
        for doc_id, score in sorted_docs:
            data = doc_data[doc_id]
            results.append(FusionResult(
                doc_id=doc_id,
                content=data["content"],
                score=score,
                source="hybrid",
                metadata=data["metadata"],
            ))

        return results


class WeightedSumFusion:
    """加权求和融合"""

    def __init__(self, weights: Optional[List[float]] = None):
        self.weights = weights

    def fuse(
        self,
        result_lists: List[List[Dict]],
        source_names: Optional[List[str]] = None,
    ) -> List[FusionResult]:
        if not source_names:
            source_names = [f"source_{i}" for i in range(len(result_lists))]

        if not self.weights:
            self.weights = [1.0 / len(result_lists)] * len(result_lists)

        scores: Dict[str, float] = {}
        doc_data: Dict[str, Dict] = {}

        for results, source_name, weight in zip(result_lists, source_names, self.weights):
            for doc in results:
                doc_id = doc.get("doc_id", doc.get("id", ""))
                if not doc_id:
                    continue

                score = doc.get("score", 0.0) * weight
                scores[doc_id] = scores.get(doc_id, 0.0) + score

                if doc_id not in doc_data:
                    doc_data[doc_id] = {
                        "doc_id": doc_id,
                        "content": doc.get("content", ""),
                        "metadata": doc.get("metadata", {}),
                        "source": source_name,
                    }

        sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        return [
            FusionResult(
                doc_id=doc_id,
                content=doc_data[doc_id]["content"],
                score=score,
                source="hybrid",
                metadata=doc_data[doc_id]["metadata"],
            )
            for doc_id, score in sorted_docs
        ]


class ConvexCombinationFusion:
    """凸组合融合"""

    def __init__(self, alpha: float = 0.5):
        self.alpha = alpha

    def fuse(
        self,
        result_lists: List[List[Dict]],
        source_names: Optional[List[str]] = None,
    ) -> List[FusionResult]:
        # 简化实现：使用加权求和
        weights = [self.alpha, 1 - self.alpha] if len(result_lists) == 2 else None
        return WeightedSumFusion(weights).fuse(result_lists, source_names)


class CombMNZFusion:
    """CombMNZ 融合"""

    def fuse(
        self,
        result_lists: List[List[Dict]],
        source_names: Optional[List[str]] = None,
    ) -> List[FusionResult]:
        if not source_names:
            source_names = [f"source_{i}" for i in range(len(result_lists))]

        scores: Dict[str, float] = {}
        appearance_count: Dict[str, int] = {}
        doc_data: Dict[str, Dict] = {}

        for results, source_name in zip(result_lists, source_names):
            for doc in results:
                doc_id = doc.get("doc_id", doc.get("id", ""))
                if not doc_id:
                    continue

                score = doc.get("score", 0.0)
                scores[doc_id] = scores.get(doc_id, 0.0) + score
                appearance_count[doc_id] = appearance_count.get(doc_id, 0) + 1

                if doc_id not in doc_data:
                    doc_data[doc_id] = {
                        "doc_id": doc_id,
                        "content": doc.get("content", ""),
                        "metadata": doc.get("metadata", {}),
                        "source": source_name,
                    }

        # CombMNZ: score * appearance_count
        final_scores = {
            doc_id: scores[doc_id] * appearance_count[doc_id]
            for doc_id in scores
        }

        sorted_docs = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)

        return [
            FusionResult(
                doc_id=doc_id,
                content=doc_data[doc_id]["content"],
                score=score,
                source="hybrid",
                metadata=doc_data[doc_id]["metadata"],
            )
            for doc_id, score in sorted_docs
        ]


def create_fusion(method: str = "rrf", **kwargs):
    """
    创建融合算法实例

    Args:
        method: 融合方法 ("rrf", "weighted", "convex", "combmnz")
        **kwargs: 算法参数

    Returns:
        融合算法实例
    """
    fusion_map = {
        "rrf": RRFFusion,
        "weighted": WeightedSumFusion,
        "convex": ConvexCombinationFusion,
        "combmnz": CombMNZFusion,
    }

    fusion_class = fusion_map.get(method, RRFFusion)
    return fusion_class(**kwargs)
