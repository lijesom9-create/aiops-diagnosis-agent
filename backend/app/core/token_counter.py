"""
Token Counter - 上下文 Token 计数与预算控制

P1-2 优化：使用 tiktoken 计算 LLM 上下文 token 数，超限时按优先级截断。

优先级（从高到低保留）：
1. 用户当前查询（必保留）
2. 系统提示（必保留）
3. 核心记忆（用户画像，必保留）
4. RAG 知识（最新召回，价值最高）
5. 档案记忆（相关记忆）
6. 对话历史（从最早开始丢弃）

支持的编码：
- cl100k_base: GPT-4 / GPT-3.5 / DeepSeek / Qwen 推荐
- o200k_base: GPT-4o 系列
- p50k_base: text-davinci-003 / Codex
"""

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# 模型 → 编码器映射（简化版）
_MODEL_TO_ENCODING = {
    # GPT-4o 系列
    "gpt-4o": "o200k_base",
    "gpt-4o-mini": "o200k_base",
    # GPT-4 / GPT-3.5 系列
    "gpt-4": "cl100k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-3.5-turbo": "cl100k_base",
    "gpt-35-turbo": "cl100k_base",
    # DeepSeek（兼容 OpenAI，cl100k 估算略偏少但可接受）
    "deepseek-chat": "cl100k_base",
    "deepseek-coder": "cl100k_base",
    "deepseek-reasoner": "cl100k_base",
    # 通义千问
    "qwen-turbo": "cl100k_base",
    "qwen-plus": "cl100k_base",
    "qwen-max": "cl100k_base",
    # 智谱
    "glm-4": "cl100k_base",
    "glm-4-flash": "cl100k_base",
    "glm-4-plus": "cl100k_base",
}


# 默认编码器（DeepSeek/Qwen 等中文场景用 cl100k_base）
_DEFAULT_ENCODING = "cl100k_base"


class TokenCounter:
    """
    Token 计数器

    使用 tiktoken 进行精确的 token 计数。
    失败时降级为字符数估算（中文 1 字符 ≈ 1.5 token，英文 4 字符 ≈ 1 token）。
    """

    _encoders: Dict[str, Any] = {}  # 模块级编码器缓存

    def __init__(self, model: Optional[str] = None, encoding: Optional[str] = None):
        """
        Args:
            model: 模型名（用于推断编码器）
            encoding: 直接指定编码器（优先于 model）
        """
        self.encoding_name = encoding or self._resolve_encoding(model)
        self._encoder = self._load_encoder(self.encoding_name)

    @staticmethod
    def _resolve_encoding(model: Optional[str]) -> str:
        """根据模型名推断编码器"""
        if not model:
            return _DEFAULT_ENCODING
        model_lower = model.lower()
        # 精确匹配
        if model_lower in _MODEL_TO_ENCODING:
            return _MODEL_TO_ENCODING[model_lower]
        # 模糊匹配
        for prefix, enc in [
            ("gpt-4o", "o200k_base"),
            ("gpt-4", "cl100k_base"),
            ("gpt-3.5", "cl100k_base"),
            ("deepseek", "cl100k_base"),
            ("qwen", "cl100k_base"),
            ("glm", "cl100k_base"),
            ("claude", "cl100k_base"),  # Anthropic Claude 也近似用 cl100k
        ]:
            if model_lower.startswith(prefix):
                return enc
        return _DEFAULT_ENCODING

    @staticmethod
    def _load_encoder(encoding_name: str):
        """加载 tiktoken 编码器（带缓存，失败时降级）"""
        if encoding_name in TokenCounter._encoders:
            return TokenCounter._encoders[encoding_name]

        try:
            import tiktoken
            enc = tiktoken.get_encoding(encoding_name)
            TokenCounter._encoders[encoding_name] = enc
            logger.debug(f"tiktoken 编码器加载: {encoding_name}")
            return enc
        except Exception as e:
            logger.warning(f"tiktoken 加载失败 ({encoding_name})，降级为字符估算: {e}")
            return None

    def count(self, text: str) -> int:
        """计算文本的 token 数"""
        if not text:
            return 0

        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text))
            except Exception:
                pass

        # 降级：字符估算
        # 中文按 1.5 字符/token，英文按 4 字符/token
        return self._estimate_by_chars(text)

    @staticmethod
    def _estimate_by_chars(text: str) -> int:
        """字符估算（tiktoken 不可用时降级）"""
        if not text:
            return 0
        import re
        # 中文字符数
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        # 非中文字符数
        non_chinese = len(text) - chinese_chars
        # 估算
        return int(chinese_chars * 1.5 + non_chinese / 4)

    def count_messages(self, messages: List[Dict[str, str]]) -> int:
        """计算消息列表的 token 数（包含每条消息的 overhead）"""
        total = 0
        for msg in messages:
            # 每条消息约 4 token overhead（role + 结构）
            total += 4
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                # 多模态消息（图片+文本）
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += self.count(part.get("text", ""))
        return total

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """
        按 token 预算截断文本（从尾部截断，保留前半部分）

        Args:
            text: 输入文本
            max_tokens: 最大 token 数

        Returns:
            截断后的文本（可能比 max_tokens 略少）
        """
        if not text or max_tokens <= 0:
            return ""

        if self._encoder is not None:
            try:
                tokens = self._encoder.encode(text)
                if len(tokens) <= max_tokens:
                    return text
                # 截断 + 解码
                truncated_tokens = tokens[:max_tokens]
                return self._encoder.decode(truncated_tokens)
            except Exception:
                pass

        # 降级：按字符截断
        return self._truncate_by_chars(text, max_tokens)

    @staticmethod
    def _truncate_by_chars(text: str, max_tokens: int) -> str:
        """按字符截断（估算）"""
        if not text:
            return ""
        # 粗略估算：每 token 约 2 字符
        max_chars = max_tokens * 2
        if len(text) <= max_chars:
            return text
        return text[:max_chars]


# 模块级单例
_default_counter: Optional[TokenCounter] = None


def get_token_counter(model: Optional[str] = None) -> TokenCounter:
    """获取默认 TokenCounter（模块级单例）"""
    global _default_counter
    if _default_counter is None or model is not None:
        if model is not None:
            # 如果指定了模型，新建（避免影响单例）
            return TokenCounter(model=model)
        _default_counter = TokenCounter()
    return _default_counter


def count_tokens(text: str, model: Optional[str] = None) -> int:
    """快捷函数：计算 text 的 token 数"""
    return get_token_counter(model).count(text)


def truncate_to_tokens(text: str, max_tokens: int, model: Optional[str] = None) -> str:
    """快捷函数：按 token 预算截断"""
    return get_token_counter(model).truncate_to_tokens(text, max_tokens)


# ========== 上下文预算控制工具 ==========


def fit_parts_to_budget(
    parts: List[Dict[str, Any]],
    max_tokens: int,
    counter: Optional[TokenCounter] = None,
    reserved_for_query: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    按 token 预算组装上下文 parts

    每个 part 是 {"name": str, "content": str, "priority": int, "truncatable": bool}
    priority 越高越保留；truncatable=True 的 part 可以从尾部截断。

    策略：
    1. 按 priority 降序排序（同 priority 保持原顺序）
    2. 优先放入高优先级 part，超预算时对可截断 part 截断，否则跳过

    Args:
        parts: 待组装的 parts 列表
        max_tokens: 总 token 预算
        counter: TokenCounter 实例
        reserved_for_query: 给用户查询预留的 token 数

    Returns:
        (selected_parts, stats)
        selected_parts: 最终保留的 parts（保持原顺序）
        stats: {"total_tokens": N, "truncated": M, "dropped": K}
    """
    counter = counter or get_token_counter()
    available = max_tokens - reserved_for_query
    if available <= 0:
        return [], {"total_tokens": 0, "truncated": 0, "dropped": len(parts)}

    # 按 priority 降序排序（同 priority 保持原 index 顺序）
    indexed = list(enumerate(parts))
    indexed.sort(key=lambda x: (-x[1].get("priority", 0), x[0]))

    # 记录每个原 index 的最终状态
    kept: Dict[int, Dict[str, Any]] = {}
    total_tokens = 0
    truncated_count = 0
    dropped_count = 0

    for idx, part in indexed:
        content = part.get("content", "")
        part_tokens = counter.count(content)

        if total_tokens + part_tokens <= available:
            kept[idx] = part
            total_tokens += part_tokens
        elif part.get("truncatable", False) and part_tokens > 0:
            # 可截断：尽量填入剩余空间
            remaining = available - total_tokens
            if remaining > 50:  # 至少留 50 token 才值得截断
                truncated_content = counter.truncate_to_tokens(content, remaining)
                new_part = dict(part)
                new_part["content"] = truncated_content
                new_part["_truncated"] = True
                kept[idx] = new_part
                total_tokens += counter.count(truncated_content)
                truncated_count += 1
            else:
                dropped_count += 1
        else:
            dropped_count += 1

    # 按 idx 升序输出（保持原顺序）
    selected = [kept[i] for i in sorted(kept.keys())]

    return selected, {
        "total_tokens": total_tokens,
        "truncated": truncated_count,
        "dropped": dropped_count,
    }

