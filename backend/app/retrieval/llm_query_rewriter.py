"""
LLM MultiQuery Rewriter - 基于 LLM 的多查询重写器

参考 LangChain MultiQueryRetriever 的设计思路：
- 让 LLM 针对同一查询生成多个等价变体
- 多个变体并行检索后融合，提升召回

特性：
- 同步 httpx 调用（不依赖 asyncio event loop）
- 失败时降级到 QueryRewriter.enhanced_rewrite
- 内置 LRU 缓存（同一 query 不重复调用 LLM）
- 可选叠加在规则重写之上（enhanced + LLM 合并去重）
"""
import json
import re
from typing import List, Optional, Callable
from functools import lru_cache
from loguru import logger


# 默认提示词模板
DEFAULT_PROMPT_TEMPLATE = """你是一个查询重写器，目标是为 RAG 文档检索生成多个等价的查询变体，以提升召回率。

输入查询：{query}

任务：为上述查询生成 3-5 个等价的检索查询变体，要求：
1. 同义词替换（中英文互换，例如"装饰器" ↔ "decorator"）
2. 缩写补全或反向缩写（例如 "ORM" ↔ "对象关系映射"）
3. 视角转换（"X 是什么" → "X 的作用/用途/原理"，"怎么用 X" → "X 使用示例"）
4. 关键词重组
5. 引入相关技术术语

输出格式要求：
- 每行一个查询
- 不要编号、不要解释、不要前后缀
- 保留原始语义，不要引入查询中未提及的无关技术
- 只输出变体本身，第一行不要重复原查询

查询变体：
"""


class LLMQueryRewriter:
    """
    基于 LLM 的查询重写器

    用法：
        rewriter = LLMQueryRewriter(provider=...)
        queries = rewriter.rewrite("装饰器怎么用", n_variants=4)
        # 返回 [原查询, ...LLM 变体] 或失败时降级到 enhanced_rewrite

    支持两种模式：
    - mode="llm"：仅 LLM 生成的变体 + 原查询
    - mode="enhanced_llm"：先规则重写，再叠加 LLM 变体（去重）

    v2 改进：
    - 纯同步 httpx 调用（避免 asyncio.run 的 event loop 残留）
    - 变体语义过滤（embedding 相似度阈值，剔除偏移变体）
    - temperature 0.2（更稳定）
    - 默认 n_variants=3（减少噪声）
    """

    # 缓存大小（同一 query 只调用一次 LLM）
    CACHE_SIZE = 256

    def __init__(
        self,
        provider=None,
        mode: str = "enhanced_llm",
        n_variants: int = 3,
        prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
        timeout: float = 30.0,
        similarity_threshold: float = 0.7,
        temperature: float = 0.2,
        embedding_model=None,
    ):
        """
        Args:
            provider: AIModelProvider 实例（None 时延迟创建）
            mode: "llm" | "enhanced_llm"
            n_variants: 期望 LLM 生成的变体数
            prompt_template: 提示词模板，必须包含 {query}
            timeout: LLM 调用超时
            similarity_threshold: 变体与原 query 的 embedding 相似度阈值，低于此值的变体被过滤
            temperature: LLM 采样温度（低 = 更稳定）
            embedding_model: 用于语义过滤的 EmbeddingModel 实例（None 时跳过过滤）
        """
        self._provider = provider
        self.mode = mode
        self.n_variants = n_variants
        self.prompt_template = prompt_template
        self.timeout = timeout
        self.similarity_threshold = similarity_threshold
        self.temperature = temperature
        self._embedding_model = embedding_model
        # 统计信息
        self.stats = {"called": 0, "success": 0, "fallback": 0, "filtered": 0}

    def set_embedding_model(self, embedding_model) -> None:
        """延迟注入 embedding_model（用于变体语义过滤）"""
        self._embedding_model = embedding_model

    def _get_provider(self):
        """延迟创建 provider（避免 import 时初始化）"""
        if self._provider is None:
            from ..core.ai_service import create_ai_provider
            self._provider = create_ai_provider()
        return self._provider

    @lru_cache(maxsize=CACHE_SIZE)
    def _call_llm(self, query: str) -> tuple:
        """调用 LLM 生成变体（带 LRU 缓存，纯同步 httpx）

        Returns:
            tuple: (variants_list, success_bool)
        """
        self.stats["called"] += 1
        try:
            variants = self._call_llm_sync(query)
            if not variants:
                return (), False
            self.stats["success"] += 1
            return tuple(variants), True
        except Exception as e:
            logger.warning(f"LLM MultiQuery 调用失败，将降级到规则重写: {type(e).__name__}: {str(e)[:200]}")
            self.stats["fallback"] += 1
            return (), False

    def _call_llm_sync(self, query: str) -> List[str]:
        """同步 httpx 调用 LLM（统一走这条路径，避免 asyncio event loop 问题）"""
        import httpx
        from ..core.config import settings
        from ..core.ai_service import parse_model_name, PROVIDER_CONFIGS

        if not settings.AI_API_KEY:
            return []

        provider_name, model_name = parse_model_name(settings.AI_MODEL)
        config = PROVIDER_CONFIGS.get(provider_name)
        if not config:
            return []
        base_url = settings.AI_BASE_URL or config["base_url"]

        prompt = self.prompt_template.format(query=query)
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "你是一个 RAG 查询重写器，只输出查询变体本身，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": 300,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.AI_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return self._parse_variants(content)

    @staticmethod
    def _parse_variants(content: str) -> List[str]:
        """解析 LLM 输出，每行一个查询"""
        if not content:
            return []
        lines = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            # 去掉可能的编号前缀
            line = re.sub(r"^\s*[\d\-•*.]+[\.\)、\s]+", "", line)
            # 去掉可能的引号
            line = line.strip("\"'`")
            if line:
                lines.append(line)
        return lines

    def _filter_variants_by_similarity(
        self,
        original_query: str,
        variants: List[str],
    ) -> List[str]:
        """用 embedding 相似度过滤变体

        如果变体与原 query 的 cosine 相似度低于 similarity_threshold，则剔除。
        embedding_model 未设置时跳过过滤。
        """
        if not variants or self._embedding_model is None:
            return variants

        try:
            import numpy as np
            orig_vec = self._embedding_model.embed(original_query)
            kept = []
            for v in variants:
                v_vec = self._embedding_model.embed(v)
                # cosine 相似度
                dot = float(np.dot(orig_vec, v_vec))
                norm_o = float(np.linalg.norm(orig_vec))
                norm_v = float(np.linalg.norm(v_vec))
                if norm_o == 0 or norm_v == 0:
                    continue
                sim = dot / (norm_o * norm_v)
                if sim >= self.similarity_threshold:
                    kept.append(v)
                else:
                    self.stats["filtered"] += 1
                    logger.debug(f"变体被过滤 (sim={sim:.3f} < {self.similarity_threshold}): {v}")
            return kept if kept else variants  # 全部过滤则保留原变体（避免空）
        except Exception as e:
            logger.debug(f"变体过滤失败，跳过: {e}")
            return variants

    def rewrite(self, query: str) -> List[str]:
        """
        重写查询，返回多个变体（包含原查询）

        Args:
            query: 原始查询

        Returns:
            List[str]: 查询变体列表（第一个总是原查询）
        """
        if not query or not query.strip():
            return [query]

        base_query = query.strip()
        queries = [base_query]

        # 1. 规则重写（始终先做）
        if self.mode in ("enhanced_llm", "enhanced"):
            try:
                from .unified_store_imports import _safe_enhanced_rewrite
                rule_variants = _safe_enhanced_rewrite(base_query)
            except ImportError:
                from ..knowledge.unified_store import QueryRewriter
                rule_variants = QueryRewriter.enhanced_rewrite(base_query)
            for q in rule_variants:
                if q not in queries:
                    queries.append(q)

        # 2. LLM 重写（仅 llm / enhanced_llm 模式）
        if self.mode in ("llm", "enhanced_llm"):
            variants, ok = self._call_llm(base_query)
            if ok:
                # 变体语义过滤：剔除与原 query 相似度过低的变体
                variants = self._filter_variants_by_similarity(base_query, list(variants))
                for v in variants:
                    v = v.strip()
                    if v and v not in queries:
                        queries.append(v)
                # 限制变体数（原查询 + 规则变体 + LLM 变体，最多 n_variants + 3）
                max_variants = self.n_variants + 3
                queries = queries[:max_variants]
            # 失败时已有规则变体兜底

        return queries

    @property
    def cache_info(self):
        return self._call_llm.cache_info()


# 模块级单例（延迟初始化）
_default_rewriter: Optional[LLMQueryRewriter] = None


def get_default_llm_rewriter() -> LLMQueryRewriter:
    """获取默认 LLM rewriter（单例）"""
    global _default_rewriter
    if _default_rewriter is None:
        _default_rewriter = LLMQueryRewriter()
    return _default_rewriter
