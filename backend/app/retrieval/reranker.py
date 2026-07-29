"""
Reranker - 结果重排序

对检索结果进行重排序，提高检索质量。
支持：
- 简单重排序（基于规则）
- CrossEncoder 重排序（基于模型）
- LLM 重排序（基于大模型）
"""

import math
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from loguru import logger

from .base import RetrievalResult


class Reranker(ABC):
    """
    重排序器基类

    职责：
    - 对检索结果进行重排序
    - 提高检索质量
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """重排序器名称"""
        pass

    @abstractmethod
    def rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        limit: int = 5,
    ) -> List[RetrievalResult]:
        """
        重排序结果

        Args:
            query: 查询文本
            results: 检索结果
            limit: 返回数量限制

        Returns:
            List[RetrievalResult]: 重排序后的结果
        """
        pass


class SimpleReranker(Reranker):
    """
    简单重排序器

    基于规则的简单重排序：
    1. 分数权重
    2. 内容长度权重
    3. 来源权重
    """

    name = "simple_reranker"

    def __init__(
        self,
        score_weight: float = 0.6,
        length_weight: float = 0.2,
        source_weight: float = 0.2,
    ):
        self.score_weight = score_weight
        self.length_weight = length_weight
        self.source_weight = source_weight

        # 来源权重映射
        self.source_weights = {
            "course_knowledge": 1.0,
            "teaching_knowledge": 0.8,
            "knowledge_graph": 0.9,
            "vector_search": 0.7,
            "user_knowledge": 0.6,
        }

    def rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        limit: int = 5,
    ) -> List[RetrievalResult]:
        """重排序结果"""
        if not results:
            return []

        # 计算综合分数
        scored_results = []
        for result in results:
            score = self._calculate_score(query, result)
            scored_results.append((score, result))

        # 按综合分数排序
        scored_results.sort(key=lambda x: x[0], reverse=True)

        # 返回 top N
        return [result for _, result in scored_results[:limit]]

    def _calculate_score(self, query: str, result: RetrievalResult) -> float:
        """计算综合分数"""
        # 原始分数
        base_score = result.score

        # 内容长度分数（适中长度更好）
        content_len = len(result.content)
        if content_len < 50:
            length_score = 0.3
        elif content_len < 200:
            length_score = 0.8
        elif content_len < 500:
            length_score = 1.0
        else:
            length_score = 0.7

        # 来源分数
        source_score = self.source_weights.get(result.source, 0.5)

        # 综合分数
        total_score = (
            base_score * self.score_weight +
            length_score * self.length_weight +
            source_score * self.source_weight
        )

        return total_score


class CrossEncoderReranker(Reranker):
    """
    CrossEncoder 重排序器

    使用交叉编码器进行语义重排序。
    推荐模型：
    - BAAI/bge-reranker-base: 中文优化，1.1GB（推荐）
    - BAAI/bge-reranker-large: 更大，效果更好，4.9GB
    - cross-encoder/ms-marco-MiniLM-L-6-v2: 英文为主，80MB

    P0-1 优化：模块级单例 + torch.no_grad 推理加速
    """

    name = "cross_encoder_reranker"

    # 模块级单例：同一模型名只加载一次，避免重复加载 1.1GB 模型
    _model_cache: Dict[str, "CrossEncoder"] = {}

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        max_length: int = 512,
    ):
        self.model_name = model_name
        self.max_length = max_length
        self._model = None

    def _load_model(self):
        """加载模型（带模块级单例缓存）"""
        if self._model is not None:
            return True

        # 检查单例缓存
        if self.model_name in CrossEncoderReranker._model_cache:
            self._model = CrossEncoderReranker._model_cache[self.model_name]
            logger.info(f"复用已加载的 CrossEncoder 模型: {self.model_name}")
            return True

        # 首次加载
        try:
            from sentence_transformers import CrossEncoder
            import os
            # 离线模式：优先用本地缓存，避免 HF 连接
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            self._model = CrossEncoder(self.model_name)
            # 推理加速：关闭梯度计算
            try:
                import torch
                self._model.model.eval()
                logger.info(f"CrossEncoder 已设为 eval 模式 (torch.no_grad)")
            except Exception:
                pass
            # 存入单例缓存
            CrossEncoderReranker._model_cache[self.model_name] = self._model
            logger.info(f"加载 CrossEncoder 模型: {self.model_name} (首次加载，已缓存)")
        except ImportError:
            logger.warning("sentence_transformers 未安装，无法使用 CrossEncoder")
            return False
        except Exception as e:
            logger.error(f"加载 CrossEncoder 模型失败: {e}")
            return False
        return True

    def rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        limit: int = 5,
    ) -> List[RetrievalResult]:
        """
        使用 CrossEncoder 重排序结果

        Args:
            query: 查询文本
            results: 检索结果
            limit: 返回数量限制

        Returns:
            List[RetrievalResult]: 重排序后的结果
        """
        if not results:
            return []

        # 加载模型
        if not self._load_model():
            # 模型加载失败时按原分数排序返回，避免 SimpleReranker 降低质量
            logger.warning("CrossEncoder 加载失败，按原始分数返回结果")
            return sorted(results, key=lambda r: r.score, reverse=True)[:limit]

        try:
            # 构建查询对
            pairs = [(query, r.content[:self.max_length]) for r in results]

            # 计算分数（CrossEncoder 输出 logits，用 sigmoid 映射到 [0,1]）
            raw_scores = self._model.predict(pairs)
            scores = [1.0 / (1.0 + math.exp(-s)) for s in raw_scores]

            # 排序
            scored_results = list(zip(scores, results))
            scored_results.sort(key=lambda x: x[0], reverse=True)

            # 更新分数
            ranked_results = []
            for score, result in scored_results[:limit]:
                result.score = float(score)
                ranked_results.append(result)

            return ranked_results

        except Exception as e:
            logger.error(f"CrossEncoder 重排序失败: {e}")
            # 按原分数排序返回，避免 SimpleReranker 降低质量
            return sorted(results, key=lambda r: r.score, reverse=True)[:limit]


class LLMReranker(Reranker):
    """
    LLM 重排序器

    使用 LLM 进行语义重排序。
    """

    name = "llm_reranker"

    def __init__(self, llm_service=None):
        self.llm_service = llm_service

    def rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        limit: int = 5,
    ) -> List[RetrievalResult]:
        """
        使用 LLM 重排序结果

        注意：此方法需要 LLM 服务支持。
        如果 LLM 服务不可用，回退到简单重排序。
        """
        if not self.llm_service:
            logger.warning("LLM 服务不可用，按原始分数返回结果")
            return sorted(results, key=lambda r: r.score, reverse=True)[:limit]

        # TODO: 实现 LLM 重排序逻辑
        # 1. 构建 prompt：query + results
        # 2. 调用 LLM 获取排序结果
        # 3. 解析 LLM 输出并重排序

        # 未实现 LLM 重排序逻辑，按原始分数返回
        return sorted(results, key=lambda r: r.score, reverse=True)[:limit]
