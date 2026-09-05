"""工具结果缓存（T9 从 tools.py 拆出）。

`_tool_cache` 为模块级单例，被 search_knowledge / web_search 及
evaluation/verify/verify_citations 使用。底层使用可插拔缓存层（app.core.cache）：
配置了 REDIS_URL → RedisCache（多 worker 共享）；未配置或连接失败 → MemoryCache（进程内）。
"""

from typing import Any, Optional

from loguru import logger


class ToolCache:
    """工具结果缓存

    底层使用可插拔缓存层（app.core.cache）：
    - 配置了 REDIS_URL → RedisCache（多 worker 共享）
    - 未配置或连接失败 → MemoryCache（进程内）
    """

    def __init__(self, ttl: int = 300):  # 默认 5 分钟过期
        from ..core.cache import get_cache
        self._backend = get_cache(ttl)

    def get(self, func_name: str, *args, **kwargs) -> Optional[Any]:
        """获取缓存"""
        result = self._backend.get(func_name, *args, **kwargs)
        if result is not None:
            logger.debug(f"缓存命中: {func_name}")
        return result

    def set(self, func_name: str, result: Any, *args, **kwargs):
        """设置缓存"""
        self._backend.set(func_name, result, *args, **kwargs)
        logger.debug(f"缓存设置: {func_name}")

    def clear(self):
        """清空缓存"""
        self._backend.clear()


# 全局缓存实例
_tool_cache = ToolCache(ttl=300)
