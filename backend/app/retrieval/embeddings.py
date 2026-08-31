"""
Embedding Models - 嵌入模型接口

将文本转换为向量表示，用于语义搜索。

实现：
- EmbeddingModel: 抽象接口
- TFIDFModel: 基于 TF-IDF 的本地嵌入（无需 API，无网络依赖）
- OpenAIEmbedding: 基于 OpenAI 兼容 API 的嵌入（需要 API Key）
"""

import os

# 在 import sentence_transformers/huggingface_hub 之前就启用离线模式，
# 避免每次加载本地模型都向 huggingface.co 发 HEAD 请求检查更新
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Dict, List, Optional

from loguru import logger


class EmbeddingModel(ABC):
    """嵌入模型抽象接口"""

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """将文本转换为向量"""
        pass

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量将文本转换为向量"""
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        """向量维度"""
        pass


class TFIDFModel(EmbeddingModel):
    """
    基于 TF-IDF 的嵌入模型

    优点：
    - 完全本地，无需 API Key
    - 无网络依赖
    - 对中文支持好（字符级分词）
    - 速度快

    缺点：
    - 不理解语义（"开心"和"高兴"无法关联）
    - 向量维度等于词汇表大小（可能很大）

    适用场景：
    - 关键词搜索增强
    - 作为 API 嵌入的降级方案
    - 小规模知识库（<10000 文档）
    """

    def __init__(self, max_features: int = 5000):
        self.max_features = max_features
        self._vocabulary: Dict[str, int] = {}
        self._idf: Dict[str, float] = {}
        self._fitted = False

    def _tokenize(self, text: str) -> List[str]:
        """
        中英文混合分词

        - 英文：按空格和标点分词
        - 中文：字符级二元组（bigram）
        """
        text = text.lower()
        # 提取英文单词
        english_words = re.findall(r'[a-z]+', text)
        # 提取中文字符
        chinese_chars = re.findall(r'[一-鿿]', text)
        # 中文二元组
        bigrams = []
        for i in range(len(chinese_chars) - 1):
            bigrams.append(chinese_chars[i] + chinese_chars[i + 1])
        # 单字也保留
        return english_words + chinese_chars + bigrams

    def fit(self, documents: List[str]) -> None:
        """
        在文档集合上训练 TF-IDF

        Args:
            documents: 文档文本列表
        """
        # 统计文档频率
        doc_freq: Dict[str, int] = Counter()
        n_docs = len(documents)

        for doc in documents:
            tokens = set(self._tokenize(doc))
            for token in tokens:
                doc_freq[token] += 1

        # 按文档频率排序，取 top N 作为词汇表
        sorted_terms = sorted(doc_freq.items(), key=lambda x: x[1], reverse=True)
        top_terms = sorted_terms[:self.max_features]

        self._vocabulary = {term: idx for idx, (term, _) in enumerate(top_terms)}

        # 计算 IDF
        self._idf = {}
        for term, freq in top_terms:
            self._idf[term] = math.log((n_docs + 1) / (freq + 1)) + 1

        self._fitted = True
        logger.info(f"TF-IDF 模型训练完成: 词汇表大小 {len(self._vocabulary)}, 文档数 {n_docs}")

    def embed(self, text: str) -> List[float]:
        """将文本转换为 TF-IDF 向量"""
        if not self._fitted:
            # 未训练时返回零向量
            return [0.0] * self.max_features

        tokens = self._tokenize(text)
        term_freq = Counter(tokens)

        vector = [0.0] * len(self._vocabulary)
        for term, freq in term_freq.items():
            if term in self._vocabulary:
                tf = freq / max(len(tokens), 1)
                idf = self._idf.get(term, 1.0)
                vector[self._vocabulary[term]] = tf * idf

        # L2 归一化
        norm = math.sqrt(sum(x * x for x in vector))
        if norm > 0:
            vector = [x / norm for x in vector]

        return vector

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量嵌入"""
        return [self.embed(text) for text in texts]

    @property
    def dimension(self) -> int:
        return len(self._vocabulary) if self._fitted else self.max_features


class OpenAIEmbedding(EmbeddingModel):
    """
    基于 OpenAI 兼容 API 的嵌入模型

    支持 OpenAI、DeepSeek、智谱等提供 embedding 接口的服务。
    """

    def __init__(self, api_key: str, model: str = "text-embedding-3-small", base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self._dimension = 1536  # text-embedding-3-small 默认维度

    def embed(self, text: str) -> List[float]:
        """调用 API 获取嵌入向量"""
        import httpx

        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                json={"input": text, "model": self.model},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]
        except Exception as e:
            logger.error(f"嵌入 API 调用失败: {e}")
            return [0.0] * self._dimension

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量调用 API"""
        import httpx

        try:
            response = httpx.post(
                f"{self.base_url}/embeddings",
                json={"input": texts, "model": self.model},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()
            # 按 index 排序
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            return [item["embedding"] for item in sorted_data]
        except Exception as e:
            logger.error(f"批量嵌入 API 调用失败: {e}")
            return [[0.0] * self._dimension] * len(texts)

    @property
    def dimension(self) -> int:
        return self._dimension


class SentenceTransformerEmbedding(EmbeddingModel):
    """
    基于 sentence-transformers 的本地语义嵌入模型

    优点：
    - 真正的语义理解（同义词、近义词可关联）
    - 本地运行，无需 API Key
    - 支持中文模型（如 bge-small-zh）

    缺点：
    - 首次加载需要下载模型
    - 占用一定内存/显存
    """

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5"):
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        logger.info(f"正在加载 sentence-transformers 模型: {model_name} (离线模式)")
        # 双保险：模块顶部已设 HF_HUB_OFFLINE，这里再显式传 local_files_only
        self._model = SentenceTransformer(model_name, local_files_only=True)
        self._dimension = self._model.get_embedding_dimension()
        logger.info(f"模型加载完成，维度: {self._dimension}")

    def embed(self, text: str) -> List[float]:
        """生成单条文本的嵌入向量"""
        try:
            return self._model.encode(text, normalize_embeddings=True).tolist()
        except Exception as e:
            logger.error(f"本地嵌入模型编码失败: {e}")
            return [0.0] * self._dimension

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成嵌入向量"""
        try:
            embeddings = self._model.encode(texts, normalize_embeddings=True)
            return embeddings.tolist()
        except Exception as e:
            logger.error(f"本地嵌入模型批量编码失败: {e}")
            return [[0.0] * self._dimension] * len(texts)

    @property
    def dimension(self) -> int:
        return self._dimension


class BGEM3Embedding(EmbeddingModel):
    """
    BGE-M3 嵌入模型：同时支持 dense + sparse 向量（同源）

    一次 encode 调用可同时获取：
    - dense_vecs: 1024 维稠密向量（语义检索）
    - lexical_weights: 稀疏向量 token_id→weight（关键词检索，替代 BM25）

    优势：
    - dense 和 sparse 同源，混合检索效果最佳
    - 多语言支持（100+ 语言），中文 sparse 表现优秀
    - 无需 jieba 分词，模型自带多语言 tokenizer

    依赖：pip install FlagEmbedding
    模型：BAAI/bge-m3（~2.2GB）
    """

    def __init__(self, model_name: str = "BAAI/bge-m3", use_fp16: bool = True):
        import torch
        from FlagEmbedding import BGEM3FlagModel

        # CPU 环境不支持 fp16：半精度权重与 fp32 输入混合运算会抛
        # "expected scalar type Half but found Float"（sparse 编码路径必现），
        # 且 CPU 上 fp16 无加速收益。无 CUDA 时自动降级 fp32，保证 sparse 向量可用。
        if use_fp16 and not torch.cuda.is_available():
            logger.warning("当前无 CUDA（CPU 环境），BGE-M3 自动降级 fp16→fp32，避免 sparse 编码失败")
            use_fp16 = False

        self.model_name = model_name
        logger.info(f"正在加载 BGE-M3 模型: {model_name} (离线模式)")
        self._model = BGEM3FlagModel(model_name, use_fp16=use_fp16)
        self._dimension = 1024  # BGE-M3 dense 固定 1024 维
        logger.info(f"BGE-M3 加载完成，dense 维度: {self._dimension}")

    def embed(self, text: str) -> List[float]:
        """生成单条文本的 dense 向量"""
        try:
            output = self._model.encode(
                [text], return_dense=True, return_sparse=False, return_colbert_vecs=False
            )
            return output["dense_vecs"][0].tolist()
        except Exception as e:
            logger.error(f"BGE-M3 dense 编码失败: {e}")
            return [0.0] * self._dimension

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成 dense 向量"""
        try:
            output = self._model.encode(
                texts, return_dense=True, return_sparse=False, return_colbert_vecs=False
            )
            return output["dense_vecs"].tolist()
        except Exception as e:
            logger.error(f"BGE-M3 批量 dense 编码失败: {e}")
            return [[0.0] * self._dimension] * len(texts)

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_sparse(self, text: str) -> dict:
        """
        生成单条文本的 sparse 向量

        Returns:
            {"indices": [int, ...], "values": [float, ...]}
        """
        try:
            output = self._model.encode(
                [text], return_dense=False, return_sparse=True, return_colbert_vecs=False
            )
            weights = output["lexical_weights"][0]
            return {"indices": list(weights.keys()), "values": list(weights.values())}
        except Exception as e:
            logger.error(f"BGE-M3 sparse 编码失败: {e}")
            return {"indices": [], "values": []}

    def embed_sparse_batch(self, texts: List[str]) -> List[dict]:
        """批量生成 sparse 向量"""
        try:
            output = self._model.encode(
                texts, return_dense=False, return_sparse=True, return_colbert_vecs=False
            )
            results = []
            for weights in output["lexical_weights"]:
                results.append({"indices": list(weights.keys()), "values": list(weights.values())})
            return results
        except Exception as e:
            logger.error(f"BGE-M3 批量 sparse 编码失败: {e}")
            return [{"indices": [], "values": []}] * len(texts)

    def embed_dense_sparse(self, text: str) -> tuple:
        """
        一次前向计算同时返回 dense + sparse（推荐，避免重复编码）

        Returns:
            (dense_vec, {"indices": [...], "values": [...]})
        """
        try:
            output = self._model.encode(
                [text], return_dense=True, return_sparse=True, return_colbert_vecs=False
            )
            dense = output["dense_vecs"][0].tolist()
            weights = output["lexical_weights"][0]
            sparse = {"indices": list(weights.keys()), "values": list(weights.values())}
            return dense, sparse
        except Exception as e:
            logger.error(f"BGE-M3 dense+sparse 编码失败: {e}")
            return [0.0] * self._dimension, {"indices": [], "values": []}

    def embed_dense_sparse_batch(self, texts: List[str]) -> tuple:
        """
        批量同时返回 dense + sparse

        Returns:
            (List[dense_vec], List[sparse_dict])
        """
        try:
            output = self._model.encode(
                texts, return_dense=True, return_sparse=True, return_colbert_vecs=False
            )
            dense_list = output["dense_vecs"].tolist()
            sparse_list = []
            for weights in output["lexical_weights"]:
                sparse_list.append({"indices": list(weights.keys()), "values": list(weights.values())})
            return dense_list, sparse_list
        except Exception as e:
            logger.error(f"BGE-M3 批量 dense+sparse 编码失败: {e}")
            return [[0.0] * self._dimension] * len(texts), [{"indices": [], "values": []}] * len(texts)


def create_embedding_model(
    api_key: Optional[str] = None,
    model_name: Optional[str] = None,
    base_url: Optional[str] = None,
    use_local_embedding: bool = True,
    local_model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    enable_cache: bool = True,
    cache_dir: Optional[str] = None,
) -> EmbeddingModel:
    """
    创建嵌入模型工厂

    优先级：
    1. API 嵌入（如果配置了 API Key 和 model_name）
    2. 本地 sentence-transformers（默认启用）
    3. TF-IDF 降级方案

    Args:
        enable_cache: 是否启用 embedding 缓存（默认 True）
        cache_dir: 磁盘缓存目录，默认 ./data/embedding_cache
    """
    if api_key and model_name:
        logger.info(f"使用 API 嵌入模型: {model_name}")
        base = OpenAIEmbedding(
            api_key=api_key,
            model=model_name,
            base_url=base_url or "https://api.openai.com/v1",
        )
    elif use_local_embedding:
        try:
            # BGE-M3 特殊处理：用 FlagEmbedding 库加载（支持 dense + sparse 同源）
            if "bge-m3" in local_model_name.lower():
                logger.info(f"使用 BGE-M3 模型 (dense+sparse 同源): {local_model_name}")
                base = BGEM3Embedding(model_name=local_model_name)
            else:
                logger.info(f"使用本地 sentence-transformer 模型: {local_model_name}")
                base = SentenceTransformerEmbedding(model_name=local_model_name)
        except Exception as e:
            logger.warning(f"本地嵌入模型加载失败，降级为 TF-IDF: {e}")
            base = TFIDFModel(max_features=5000)
    else:
        logger.info("使用本地 TF-IDF 模型")
        base = TFIDFModel(max_features=5000)

    # 默认启用缓存：查询 embedding 是热点路径，缓存可省 50-100ms/次
    if enable_cache:
        from .embedding_cache import CachedEmbeddingModel
        if cache_dir is None:
            cache_dir = "./data/embedding_cache"
        return CachedEmbeddingModel(base, cache_dir=cache_dir)

    return base
