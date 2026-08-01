"""
可插拔缓存层

提供统一的缓存接口，支持内存和 Redis 两种后端，根据配置自动选择。
- 开发环境（无 REDIS_URL）：使用 MemoryCache，零依赖
- 生产环境（有 REDIS_URL）：使用 RedisCache，多 worker 共享、重启不丢失
- Redis 连接失败时自动降级到 MemoryCache，保证可用性

使用方式：
    from app.core.cache import get_cache
    cache = get_cache()
    cache.set("ns", value, "key1", "key2")
    cached = cache.get("ns", "key1", "key2")
"""

import hashlib
import time
import json
from typing import Any, Optional, Dict
from loguru import logger

from .config import settings


class CacheBackend:
    """缓存后端抽象接口"""

    def get(self, namespace: str, *args, **kwargs) -> Optional[Any]:
        raise NotImplementedError

    def set(self, namespace: str, value: Any, *args, **kwargs):
        raise NotImplementedError

    def delete_pattern(self, namespace: str, pattern: str):
        """删除命名空间下匹配模式的所有键"""
        raise NotImplementedError

    def clear(self, namespace: Optional[str] = None):
        raise NotImplementedError

    @staticmethod
    def _make_key(namespace: str, *args, **kwargs) -> str:
        """生成缓存键"""
        key_str = f"{namespace}:{args}:{sorted(kwargs.items())}"
        return hashlib.md5(key_str.encode()).hexdigest()


class MemoryCache(CacheBackend):
    """内存缓存（开发环境 / Redis 降级后备）

    使用 OrderedDict 实现 LRU 淘汰：
    - get 命中时将 key 移到末尾（最近访问）
    - set 超过 max_size 时淘汰头部（最久未访问）
    - 防止长期运行内存无限增长
    """

    def __init__(self, ttl: int = 300, max_size: int = 1024):
        from collections import OrderedDict
        self._cache: "OrderedDict[str, Any]" = OrderedDict()
        self._ttl = ttl
        self._max_size = max_size

    def _full_key(self, namespace: str, *args, **kwargs) -> str:
        """带 namespace 前缀的键，支持按命名空间批量删除"""
        return f"{namespace}:{self._make_key(namespace, *args, **kwargs)}"

    def get(self, namespace: str, *args, **kwargs) -> Optional[Any]:
        key = self._full_key(namespace, *args, **kwargs)
        if key in self._cache:
            result, timestamp = self._cache[key]
            if time.time() - timestamp < self._ttl:
                # LRU：命中后移到末尾（最近访问）
                self._cache.move_to_end(key)
                return result
            else:
                del self._cache[key]
        return None

    def set(self, namespace: str, value: Any, *args, **kwargs):
        key = self._full_key(namespace, *args, **kwargs)
        # 已存在则更新（并移到末尾）；新增则可能触发淘汰
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = (value, time.time())
        # LRU 淘汰：超过 max_size 时删除头部（最久未访问）
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def delete_pattern(self, namespace: str, pattern: str):
        """删除命名空间下的所有键"""
        prefix = f"{namespace}:"
        keys_to_delete = [k for k in self._cache if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._cache[k]

    def clear(self, namespace: Optional[str] = None):
        if namespace:
            self.delete_pattern(namespace, "*")
        else:
            self._cache.clear()


class RedisCache(CacheBackend):
    """Redis 缓存（生产环境）

    使用同步 redis 客户端（与现有同步工具链兼容）。
    序列化用 JSON（缓存的都是 dict/list/str 等基础类型）。
    """

    def __init__(self, redis_url: str, ttl: int = 300):
        import redis
        self._redis = redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl
        self._prefix = "rag:"  # 所有键加前缀，避免与其他服务冲突
        logger.info(f"Redis 缓存已启用: {redis_url}")

    def _full_key(self, namespace: str, *args, **kwargs) -> str:
        return f"{self._prefix}{namespace}:{self._make_key(namespace, *args, **kwargs)}"

    def get(self, namespace: str, *args, **kwargs) -> Optional[Any]:
        key = self._full_key(namespace, *args, **kwargs)
        try:
            raw = self._redis.get(key)
            if raw is not None:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"Redis GET 失败（降级处理）: {e}")
        return None

    def set(self, namespace: str, value: Any, *args, **kwargs):
        key = self._full_key(namespace, *args, **kwargs)
        try:
            self._redis.setex(key, self._ttl, json.dumps(value, ensure_ascii=False, default=str))
        except Exception as e:
            logger.debug(f"Redis SET 失败（降级处理）: {e}")

    def delete_pattern(self, namespace: str, pattern: str):
        """删除匹配模式的所有键（用于文档更新/删除时失效缓存）"""
        full_pattern = f"{self._prefix}{namespace}:*"
        try:
            keys = self._redis.keys(full_pattern)
            if keys:
                self._redis.delete(*keys)
        except Exception as e:
            logger.debug(f"Redis DELETE 失败: {e}")

    def clear(self, namespace: Optional[str] = None):
        if namespace:
            self.delete_pattern(namespace, "*")
        else:
            try:
                keys = self._redis.keys(f"{self._prefix}*")
                if keys:
                    self._redis.delete(*keys)
            except Exception as e:
                logger.debug(f"Redis CLEAR 失败: {e}")


# ========== 单例缓存实例 ==========

_cache_instance: Optional[CacheBackend] = None


def get_cache(ttl: int = 300) -> CacheBackend:
    """获取缓存实例（单例）

    根据 settings.REDIS_URL 决定后端：
    - 有 REDIS_URL：尝试连接 Redis，成功则用 RedisCache
    - 无 REDIS_URL 或连接失败：降级到 MemoryCache

    Args:
        ttl: 缓存过期时间（秒），仅首次调用生效

    Returns:
        CacheBackend 实例
    """
    global _cache_instance
    if _cache_instance is not None:
        return _cache_instance

    redis_url = getattr(settings, "REDIS_URL", None)
    if redis_url:
        try:
            _cache_instance = RedisCache(redis_url, ttl=ttl)
            # 测试连接
            _cache_instance._redis.ping()
            logger.info("Redis 连接成功，使用 Redis 缓存")
        except Exception as e:
            logger.warning(f"Redis 连接失败，降级到内存缓存: {e}")
            _cache_instance = MemoryCache(ttl=ttl)
    else:
        logger.info("未配置 REDIS_URL，使用内存缓存")
        _cache_instance = MemoryCache(ttl=ttl)

    return _cache_instance


def reset_cache():
    """重置缓存实例（用于测试）"""
    global _cache_instance
    _cache_instance = None
