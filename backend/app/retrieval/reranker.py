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
        max_length: int = 512,
        enable_cache: bool = True,
        cache_capacity: int = 1024,
        use_onnx: bool = True,
        # ---- MaxP 切块聚合（P2 优化：解决长文档重排截断丢失信息）----
        # 本地 CrossEncoder 无法处理长文档（token 上限 max_length），
        # 旧实现直接 content[:max_length] 字符级硬截断，只看到文档开头 ~4% 内容。
        # 改为：长文本切块 + 每块打分 + MaxP（取最高块分）聚合，
        # 参考 BERT-MaxP (Dai & Callan, SIGIR'19) 与 Cohere max_chunks_per_doc。
        maxp_enabled: bool = True,
        # bge-reranker-base 支持 512 token（中文约 2 字符/token），
        # max_length=512 + chunk_size=400 字符 ≈ 200 token，为 query 预留充足空间
        maxp_chunk_size: int = 400,      # 切块字符长度（v2：200→400，块长翻倍减少块数，覆盖不变）
        maxp_chunk_overlap: int = 100,   # 相邻块重叠字符数（v2：50→100，与块长同比例）
        maxp_aggregation: str = "max",   # "max"（MaxP，推荐）| "mean"（平均池化）
        max_chunks_per_doc: int = 8,     # v2：16→8，推理次数减半（256→128），覆盖 400+7*300=2500 字符不变
    ):
        """
        Args:
            use_onnx: 是否启用 ONNX Runtime 加速（CPU 推理快 2-3x，精度无损）
                      首次加载会自动转换并缓存到磁盘，后续启动直接加载 ONNX 模型
            maxp_enabled: 是否启用长文本切块 + MaxP 聚合（短文本不受影响，走单块路径）
            maxp_chunk_size: 切块字符长度。bge-reranker 中文约 2 字符 ≈ 1 token，
                             400 字符 ≈ 200 token + query 后仍低于 max_length=512 token 上限。
            maxp_aggregation: 块分数聚合策略。MaxP 保留"闪光点"（查询只与文档某部分相关时最优）；
                             mean 会稀释相关信号（BReps 论文实测 MaxP 显著优于 AvgP）。
        """
        self.model_name = model_name
        self.max_length = max_length
        self._model = None
        self._tokenizer = None  # ONNX 模式下单独使用
        self._use_onnx = use_onnx
        self._onnx_ready = False  # ONNX 模型是否加载成功
        self._enable_cache = enable_cache
        # (query_hash, chunk_hash) -> score，chunk 为切块后文本（短文本即全文）
        self._score_cache: Dict[tuple, float] = {}
        self._cache_capacity = cache_capacity
        self._cache_hits = 0
        self._cache_misses = 0

        # MaxP 切块聚合配置
        self.maxp_enabled = maxp_enabled
        self.maxp_chunk_size = max(32, int(maxp_chunk_size))
        self.maxp_chunk_overlap = max(0, min(int(maxp_chunk_overlap), self.maxp_chunk_size - 1))
        self.maxp_aggregation = maxp_aggregation if maxp_aggregation == "mean" else "max"
        self.max_chunks_per_doc = max(1, int(max_chunks_per_doc))

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

        # 加载已缓存的 ONNX 模型（优先 int8 量化版，CPU 推理快 2-4x，精度损失极小）
        import onnxruntime as ort
        from optimum.onnxruntime import ORTModelForSequenceClassification

        model_file = "model_int8.onnx"
        if not (onnx_dir / model_file).exists():
            model_file = "model.onnx"

        # 构造 session options：intra 线程数 = 物理核一半（避免与向量模型并发竞争），
        # 旧 API set_intra_op_num_threads 在 ORT >=1.19 已移除，必须通过 SessionOptions 设置
        sess_options = ort.SessionOptions()
        _n_cores = os.cpu_count() or 8
        sess_options.intra_op_num_threads = max(4, _n_cores // 2)
        sess_options.inter_op_num_threads = 1
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        logger.info(f"加载 ONNX 模型: {onnx_dir / model_file} (intra={sess_options.intra_op_num_threads})")
        self._model = ORTModelForSequenceClassification.from_pretrained(
            str(onnx_dir),
            file_name=model_file,
            provider="CPUExecutionProvider",
            session_options=sess_options,
            use_io_binding=False,
        )
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

    def _split_chunks(self, text: str) -> List[str]:
        """
        将长文本切成重叠滑动窗口块。

        规则：
        - 短文本（<= maxp_chunk_size）返回单块，直接整段打分（tokenizer 内部处理截断）
        - 长文本按 stride = chunk_size - overlap 滑动切块，相邻块 25% 重叠，缓解语义被切碎
        - 若块数超出 max_chunks_per_doc，自动增大 stride 压缩块数，同时保证覆盖全文
          （参考 Cohere rerank 的 max_chunks_per_doc 上限思想）
        """
        n = len(text)
        if n <= self.maxp_chunk_size:
            return [text]

        chunk_size = self.maxp_chunk_size
        stride = chunk_size - self.maxp_chunk_overlap
        if stride < 1:
            stride = 1

        # 期望块数（向上取整），超上限时增大步长
        n_chunks = 1 + (n - chunk_size + stride - 1) // stride
        if n_chunks > self.max_chunks_per_doc:
            denom = max(1, self.max_chunks_per_doc - 1)
            stride = max(1, (n - chunk_size + denom - 1) // denom)
            n_chunks = 1 + (n - chunk_size + stride - 1) // stride

        chunks: List[str] = []
        pos = 0
        while pos < n:
            end = min(pos + chunk_size, n)
            chunks.append(text[pos:end])
            if end == n:
                break
            pos += stride
        return chunks

    def rerank(
        self,
        query: str,
        results: List[RetrievalResult],
        limit: int = 5,
    ) -> List[RetrievalResult]:
        """
        使用 CrossEncoder 重排序结果

        长文档处理（P2 优化）：
        旧实现 content[:max_length] 字符级硬截断，88% 的运维父块（中位 6686 字符）只被
        看到开头 256 字符（约 4%），查询相关内容在文档后段时重排分数完全失真。
        新实现：长文本按重叠窗口切块，每块与 query 单独打分，聚合为文档分数：
        - MaxP（默认）：取最高块分，保留"闪光点"，查询只需命中文档任意一段
        - Mean：取平均，适合查询与整篇主题相关（会稀释局部强相关信号）
        短文本（<= maxp_chunk_size）不走切块，行为与旧版等价（整段送入，tokenizer 截断）。

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

            query_hash = hashlib.md5(query.encode("utf-8")).hexdigest()[:8] if self._enable_cache else None

            # 1. 切块规划：(result_index, chunk_index, chunk_text, cached_score)
            plan: List[Tuple[int, int, str, Optional[float]]] = []
            for i, r in enumerate(results):
                if self.maxp_enabled:
                    chunks = self._split_chunks(r.content)
                else:
                    # 兼容旧行为：不切块，整段送入（tokenizer 内部截断到 max_length）
                    chunks = [r.content]
                for ci, chunk in enumerate(chunks):
                    cached = None
                    if self._enable_cache and query_hash is not None:
                        chunk_hash = hashlib.md5(chunk.encode("utf-8")).hexdigest()[:8]
                        cached = self._score_cache.get((query_hash, chunk_hash))
                        if cached is not None:
                            self._cache_hits += 1
                    plan.append((i, ci, chunk, cached))

            # 2. 批量推理未命中的块
            miss_positions = [k for k, p in enumerate(plan) if p[3] is None]
            if miss_positions:
                miss_pairs = [(query, plan[k][2]) for k in miss_positions]
                raw_scores = self._predict_pairs(miss_pairs)
                new_scores = [1.0 / (1.0 + math.exp(-s)) for s in raw_scores]
                for k, score in zip(miss_positions, new_scores):
                    plan[k] = (plan[k][0], plan[k][1], plan[k][2], score)
                    self._cache_misses += 1
                    if self._enable_cache and query_hash is not None:
                        chunk_hash = hashlib.md5(plan[k][2].encode("utf-8")).hexdigest()[:8]
                        cache_key = (query_hash, chunk_hash)
                        if len(self._score_cache) < self._cache_capacity:
                            self._score_cache[cache_key] = score

            # 3. 按文档聚合块分数（MaxP / Mean）
            doc_chunk_scores: Dict[int, List[float]] = {}
            for i, _, _, score in plan:
                if score is not None:
                    doc_chunk_scores.setdefault(i, []).append(score)
            doc_scores: Dict[int, float] = {}
            for i, scores in doc_chunk_scores.items():
                if self.maxp_aggregation == "mean":
                    doc_scores[i] = sum(scores) / len(scores)
                else:
                    doc_scores[i] = max(scores)

            # 4. 排序并回填分数
            ranked_idx = sorted(
                range(len(results)),
                key=lambda i: doc_scores.get(i, float("-inf")),
                reverse=True,
            )[:limit]
            ranked_results = []
            for i in ranked_idx:
                results[i].score = float(doc_scores.get(i, results[i].score))
                ranked_results.append(results[i])
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
