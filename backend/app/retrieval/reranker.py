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
from typing import Dict, List, Optional, Tuple
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
    P1 优化：相同 (query, doc) 对的打分缓存，避免重复推理
    """

    name = "cross_encoder_reranker"

    # 模块级单例：同一模型名只加载一次，避免重复加载 1.1GB 模型
    _model_cache: Dict[str, "CrossEncoder"] = {}

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-base",
        max_length: int = 256,
        enable_cache: bool = True,
        cache_capacity: int = 1024,
        use_onnx: bool = True,
    ):
        """
        Args:
            use_onnx: 是否启用 ONNX Runtime 加速（CPU 推理快 2-3x，精度无损）
                      首次加载会自动转换并缓存到磁盘，后续启动直接加载 ONNX 模型
        """
        self.model_name = model_name
        self.max_length = max_length
        self._model = None
        self._tokenizer = None  # ONNX 模式下单独使用
        self._use_onnx = use_onnx
        self._onnx_ready = False  # ONNX 模型是否加载成功
        self._enable_cache = enable_cache
        # (query_hash, content_hash) -> score
        self._score_cache: Dict[tuple, float] = {}
        self._cache_capacity = cache_capacity
        self._cache_hits = 0
        self._cache_misses = 0

    def _load_model(self):
        """加载模型（带模块级单例缓存）"""
        if self._model is not None:
            return True

        # 检查单例缓存
        cache_key = f"{self.model_name}{'_onnx' if self._use_onnx else ''}"
        if cache_key in CrossEncoderReranker._model_cache:
            cached = CrossEncoderReranker._model_cache[cache_key]
            self._model = cached["model"]
            self._tokenizer = cached.get("tokenizer")
            self._onnx_ready = cached.get("onnx", False)
            logger.info(f"复用已加载的 CrossEncoder 模型: {cache_key}")
            return True

        # 首次加载：优先尝试 ONNX，失败降级到 PyTorch
        if self._use_onnx:
            try:
                self._load_onnx_model(cache_key)
                return True
            except Exception as e:
                logger.warning(f"ONNX 加载失败，降级到 PyTorch: {type(e).__name__}: {e}")

        # PyTorch 降级路径
        return self._load_pytorch_model(cache_key)

    def _load_onnx_model(self, cache_key: str) -> bool:
        """加载 ONNX 格式模型（首次自动转换并缓存到磁盘）"""
        import os
        from pathlib import Path
        from transformers import AutoTokenizer

        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        # ONNX 模型缓存目录：./data/onnx_cache/<model_name>
        model_dir_name = self.model_name.replace("/", "_")
        onnx_dir = Path("./data/onnx_cache") / model_dir_name

        # 首次：用 optimum 从 PyTorch 模型导出 ONNX
        if not onnx_dir.exists():
            logger.info(f"首次导出 ONNX 模型: {self.model_name} -> {onnx_dir}")
            from optimum.onnxruntime import ORTModelForSequenceClassification

            onnx_dir.mkdir(parents=True, exist_ok=True)
            # export=True 会自动转换并保存到 onnx_dir
            model = ORTModelForSequenceClassification.from_pretrained(
                self.model_name,
                export=True,
                provider="CPUExecutionProvider",
            )
            model.save_pretrained(str(onnx_dir))
            logger.info(f"ONNX 模型导出完成: {onnx_dir}")

        # 加载已缓存的 ONNX 模型
        from optimum.onnxruntime import ORTModelForSequenceClassification
        # P0-2 优化：限制 ORT 线程数，避免并发时线程竞争
        import os
        os.environ.setdefault("ORT_NUM_THREADS", "2")
        logger.info(f"加载 ONNX 模型: {onnx_dir}")
        self._model = ORTModelForSequenceClassification.from_pretrained(
            str(onnx_dir),
            provider="CPUExecutionProvider",
            use_io_binding=False,
        )
        # 设置 ORT session 线程数
        try:
            session = self._model.model  # 底层 ort.InferenceSession
            session.set_intra_op_num_threads(2)
            session.set_inter_op_num_threads(1)
            logger.info("ONNX 线程数: intra=2, inter=1")
        except Exception as e:
            logger.debug(f"ORT线程设置跳过: {e}")
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self._onnx_ready = True

        # 存入单例缓存
        CrossEncoderReranker._model_cache[cache_key] = {
            "model": self._model,
            "tokenizer": self._tokenizer,
            "onnx": True,
        }
        logger.info(f"ONNX CrossEncoder 加载完成: {self.model_name}")
        return True

    def _load_pytorch_model(self, cache_key: str) -> bool:
        """PyTorch 降级加载路径"""
        try:
            from sentence_transformers import CrossEncoder
            import os
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            self._model = CrossEncoder(self.model_name)
            try:
                import torch
                self._model.model.eval()
                logger.info(f"CrossEncoder 已设为 eval 模式 (torch.no_grad)")
            except Exception:
                pass
            CrossEncoderReranker._model_cache[cache_key] = {
                "model": self._model,
                "tokenizer": None,
                "onnx": False,
            }
            logger.info(f"加载 PyTorch CrossEncoder 模型: {self.model_name} (首次加载，已缓存)")
        except ImportError:
            logger.warning("sentence_transformers 未安装，无法使用 CrossEncoder")
            return False
        except Exception as e:
            logger.error(f"加载 CrossEncoder 模型失败: {e}")
            return False
        return True

    def _predict_pairs(self, pairs: List[Tuple[str, str]]) -> List[float]:
        """
        对 (query, content) 对批量打分

        ONNX 模式：用 tokenizer 编码 + ONNX 模型推理（快 2-3x）
        PyTorch 模式：直接调用 CrossEncoder.predict()
        """
        if self._onnx_ready and self._tokenizer is not None:
            # ONNX 推理路径
            import torch
            encoded = self._tokenizer(
                pairs,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            with torch.no_grad():
                outputs = self._model(**encoded)
            # logits 是 (batch, 1) 或 (batch, 2)，取最后一列
            logits = outputs.logits.squeeze(-1)
            return logits.tolist()
        else:
            # PyTorch 推理路径（CrossEncoder.predict 内部处理 tokenizer）
            return self._model.predict(pairs)

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
            import hashlib

            # 构建查询对，并检查缓存
            query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()[:8] if self._enable_cache else None
            pairs = []
            cached_scores: List[Optional[float]] = [None] * len(results)
            miss_indices = []

            for i, r in enumerate(results):
                content_trunc = r.content[:self.max_length]
                if self._enable_cache and query_hash is not None:
                    content_hash = hashlib.md5(content_trunc.encode("utf-8")).hexdigest()[:8]
                    cache_key = (query_hash, content_hash)
                    cached = self._score_cache.get(cache_key)
                    if cached is not None:
                        cached_scores[i] = cached
                        self._cache_hits += 1
                        continue
                pairs.append((query, content_trunc))
                miss_indices.append(i)

            # 仅对未命中的批量推理
            scores = []
            if pairs:
                raw_scores = self._predict_pairs(pairs)
                new_scores = [1.0 / (1.0 + math.exp(-s)) for s in raw_scores]
                # 回填缓存
                for idx, score in zip(miss_indices, new_scores):
                    cached_scores[idx] = score
                    self._cache_misses += 1
                    if self._enable_cache and query_hash is not None:
                        content_trunc = results[idx].content[:self.max_length]
                        content_hash = hashlib.md5(content_trunc.encode("utf-8")).hexdigest()[:8]
                        cache_key = (query_hash, content_hash)
                        if len(self._score_cache) < self._cache_capacity:
                            self._score_cache[cache_key] = score

            # 排序
            scored_results = list(zip(cached_scores, results))
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
