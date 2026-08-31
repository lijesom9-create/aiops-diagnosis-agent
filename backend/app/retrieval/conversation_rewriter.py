"""
多轮对话查询改写器 (Conversation Query Rewriter)

基于三层判断的智能改写策略：
1. 规则判断：检测指代词/省略主语/上下文依赖词（0ms）
2. 历史判断：检查是否有对话历史（0ms）
3. LLM 改写：指代消解，生成独立完整查询（200ms）

参考：
- LlamaIndex CondenseQuestionChatEngine
- DH-RAG (arxiv 2502.13847)
- Dialogue-RAG (ACL 2025)
"""
import re
from typing import Dict, List, Optional

from loguru import logger

# 指代词列表（命中则可能需要改写）
REFERENCE_WORDS = {
    # 代词
    "它", "它们", "这个", "那个", "这两个", "上面", "下面",
    "他", "她", "其", "此", "该", "刚才", "之前提到的", "前面提到",
    "上文", "之前的", "上面的",
    # 英语代词
    "it", "this", "that", "these", "those", "above", "below",
}

# 上下文依赖词（命中则需要改写）
CONTEXT_DEPENDENT_WORDS = {
    "继续", "再说", "详细", "具体", "比如", "举个例子",
    "进一步", "深入", "展开", "补充", "还有呢", "然后呢",
    "为什么", "怎么办", "怎么回事",
}

# 不需要改写的完整问句模式（命中后跳过改写，避免短清晰问句被误判）
# 注意：这些模式在 needs_rewrite 中位于"指代词/上下文依赖词"检查之后，
# 因此 "它是什么" 仍会因指代词 "它" 触发改写
_CLEAR_QUERY_REGEXES = [
    re.compile(p) for p in (
        r"什么是.+", r"怎样.+", r"如何.+", r"怎么.+", r"为什么.+",
        r"哪些.+", r"列举.+", r"解释.+", r"描述.+",
        r"对比.+和.+", r"比较.+和.+",
        r"what\s+is", r"how\s+to", r"why", r"explain",
    )
]

# 改写 prompt 模板
REWRITE_PROMPT_TEMPLATE = """你是一个查询改写助手。任务是根据对话历史，将用户的追问改写为独立、完整的查询。

对话历史：
{chat_history}

用户追问：{question}

改写要求：
1. 消解指代词（"它/这个/那个" → 具体名词）
2. 补充省略的主语或宾语
3. 保留用户原始意图，不要引入新信息
4. 输出一个独立的完整查询，不要解释、不要编号
5. 如果追问本身已经完整清晰，直接输出原问题

改写后的查询："""


class ConversationQueryRewriter:
    """
    多轮对话查询改写器

    三层判断策略：
    1. 规则判断：检测指代词/省略主语/上下文依赖词
    2. 历史判断：检查是否有足够的对话历史
    3. LLM 改写：指代消解

    用法：
        rewriter = ConversationQueryRewriter()
        if rewriter.needs_rewrite(query, chat_history):
            rewritten = rewriter.rewrite(query, chat_history)
        else:
            rewritten = query  # 不需要改写
    """

    # 缓存大小（同一 query+history_hash 只调用一次 LLM）
    CACHE_SIZE = 256

    def __init__(
        self,
        provider=None,
        timeout: float = 10.0,
        temperature: float = 0.1,
        max_history_turns: int = 3,
    ):
        """
        Args:
            provider: AIModelProvider 实例（None 时延迟创建）
            timeout: LLM 调用超时
            temperature: 低温度保证稳定性
            max_history_turns: 最多使用多少轮对话历史
        """
        self._provider = provider
        self.timeout = timeout
        self.temperature = temperature
        self.max_history_turns = max_history_turns
        # 统计
        self.stats = {
            "checked": 0,
            "needs_rewrite": 0,
            "skipped_no_history": 0,
            "skipped_clear_query": 0,
            "llm_called": 0,
            "llm_success": 0,
            "llm_failed": 0,
        }

    def _get_provider(self):
        """延迟创建 provider"""
        if self._provider is None:
            from ..core.ai_service import create_ai_provider
            self._provider = create_ai_provider()
        return self._provider

    def needs_rewrite(self, query: str, chat_history: Optional[List[Dict]]) -> bool:
        """
        三层判断：是否需要改写

        Args:
            query: 用户查询
            chat_history: 对话历史 [{role, content}, ...]

        Returns:
            bool: True 表示需要 LLM 改写
        """
        self.stats["checked"] += 1

        # 第二层：没有历史，不需要改写
        if not chat_history or len(chat_history) < 2:
            self.stats["skipped_no_history"] += 1
            return False

        query_lower = query.lower().strip()

        # 第一层：规则判断

        # 1. 检测指代词（强信号：只要出现就需要改写）
        for word in REFERENCE_WORDS:
            if word in query_lower:
                self.stats["needs_rewrite"] += 1
                return True

        # 2. 检测上下文依赖词（强信号）
        for word in CONTEXT_DEPENDENT_WORDS:
            if word in query_lower:
                self.stats["needs_rewrite"] += 1
                return True

        # 3. 清晰问句模式（如 "什么是 X", "如何 X", "列举 X"）→ 即使短也不改写
        #    避免 "什么是 RAG" 等短清晰问句被误判
        if any(pattern.search(query) for pattern in _CLEAR_QUERY_REGEXES):
            self.stats["skipped_clear_query"] += 1
            return False

        # 4. 检测省略主语（短查询且无名词）
        if len(query) < 10:
            has_noun = self._has_noun(query)
            if not has_noun:
                self.stats["needs_rewrite"] += 1
                return True

        # 5. 不匹配任何需要改写的模式 → 清晰 query
        self.stats["skipped_clear_query"] += 1
        return False

    def _has_noun(self, query: str) -> bool:
        """检查查询是否包含名词（用 jieba 分词）"""
        try:
            import jieba.posseg as pseg
            words = list(pseg.cut(query))
            # 检查是否有名词（n 开头的词性）
            for w in words:
                if w.flag.startswith("n"):
                    return True
            return False
        except ImportError:
            # jieba 未安装，用简单规则：检查是否包含常见名词后缀
            noun_suffixes = ["器", "法", "量", "库", "表", "型", "件", "程", "码", "据"]
            return any(suffix in query for suffix in noun_suffixes)

    def _format_history(self, chat_history: List[Dict]) -> str:
        """格式化对话历史为 prompt 文本"""
        # 只取最近 N 轮
        recent = chat_history[-(self.max_history_turns * 2):]
        lines = []
        for turn in recent:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if role == "user":
                lines.append(f"用户: {content}")
            else:
                lines.append(f"助手: {content[:100]}...")  # AI 回复截断
        return "\n".join(lines)

    def rewrite(self, query: str, chat_history: List[Dict]) -> str:
        """
        LLM 改写：指代消解

        Args:
            query: 用户查询
            chat_history: 对话历史

        Returns:
            改写后的独立查询，失败时返回原始 query
        """
        self.stats["llm_called"] += 1

        try:
            history_str = self._format_history(chat_history)
            prompt = REWRITE_PROMPT_TEMPLATE.format(
                chat_history=history_str,
                question=query,
            )

            # 同步 httpx 调用 LLM（避免 asyncio event loop 问题，与 LLMQueryRewriter 一致）
            rewritten = self._call_llm_sync(prompt)

            # 清理可能的引号、前缀
            rewritten = rewritten.strip("\"'""''")  # noqa: B005  # 有意按字符集剥离引号（非前缀）
            # 去除可能的 "改写后的查询：" 前缀
            for prefix in ["改写后的查询：", "改写后的查询:", "改写：", "改写:"]:
                if rewritten.startswith(prefix):
                    rewritten = rewritten[len(prefix):].strip()

            if rewritten and rewritten != query:
                self.stats["llm_success"] += 1
                logger.debug(f"对话改写: '{query}' → '{rewritten}'")
                return rewritten
            else:
                # LLM 认为不需要改写
                self.stats["llm_success"] += 1
                return query

        except Exception as e:
            self.stats["llm_failed"] += 1
            logger.warning(f"对话查询改写失败: {type(e).__name__}: {e}")
            return query  # 降级到原始 query

    def _call_llm_sync(self, prompt: str) -> str:
        """同步 httpx 调用 LLM（参考 LLMQueryRewriter._call_llm_sync）"""
        import httpx

        from ..core.ai_service import PROVIDER_CONFIGS, parse_model_name
        from ..core.config import settings

        if not settings.AI_API_KEY:
            return ""

        provider_name, model_name = parse_model_name(settings.AI_MODEL)
        config = PROVIDER_CONFIGS.get(provider_name)
        if not config:
            return ""
        base_url = settings.AI_BASE_URL or config["base_url"]

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "你是一个对话查询改写器，只输出改写后的独立查询本身，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": 200,
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
            return data["choices"][0]["message"]["content"].strip()

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()


# ========== 模块级单例 ==========

_default_rewriter: Optional[ConversationQueryRewriter] = None


def get_default_conversation_rewriter() -> ConversationQueryRewriter:
    """获取默认的 ConversationQueryRewriter 单例（从 settings 读取配置）"""
    global _default_rewriter
    if _default_rewriter is None:
        try:
            from ..core.config import settings
            _default_rewriter = ConversationQueryRewriter(
                timeout=settings.CONVERSATION_REWRITE_TIMEOUT,
                temperature=settings.CONVERSATION_REWRITE_TEMPERATURE,
                max_history_turns=settings.CONVERSATION_REWRITE_MAX_HISTORY_TURNS,
            )
        except Exception as e:
            logger.warning(f"读取 conversation rewriter 配置失败，使用默认值: {e}")
            _default_rewriter = ConversationQueryRewriter()
    return _default_rewriter
