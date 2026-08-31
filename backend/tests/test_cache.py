"""
缓存层单元测试

测试覆盖：
- MemoryCache 基本读写
- TTL 过期
- 命名空间隔离
- clear 按命名空间清除
- get_cache 单例
- 缓存键生成（相同参数命中，不同参数不命中）
"""

import time

from app.core.cache import MemoryCache, get_cache, reset_cache


class TestMemoryCacheBasic:
    """MemoryCache 基本读写"""

    def test_set_and_get(self):
        cache = MemoryCache(ttl=60)
        cache.set("ns1", {"key": "value"}, "arg1")
        assert cache.get("ns1", "arg1") == {"key": "value"}

    def test_get_miss(self):
        cache = MemoryCache(ttl=60)
        assert cache.get("ns1", "nonexistent") is None

    def test_set_different_namespaces(self):
        """不同命名空间互不干扰"""
        cache = MemoryCache(ttl=60)
        cache.set("ns1", "val1", "key1")
        cache.set("ns2", "val2", "key1")
        assert cache.get("ns1", "key1") == "val1"
        assert cache.get("ns2", "key1") == "val2"

    def test_set_different_keys(self):
        """同命名空间不同参数不冲突"""
        cache = MemoryCache(ttl=60)
        cache.set("ns1", "val1", "key1")
        cache.set("ns1", "val2", "key2")
        assert cache.get("ns1", "key1") == "val1"
        assert cache.get("ns1", "key2") == "val2"

    def test_set_overwrite(self):
        """同键覆盖"""
        cache = MemoryCache(ttl=60)
        cache.set("ns1", "old", "key1")
        cache.set("ns1", "new", "key1")
        assert cache.get("ns1", "key1") == "new"


class TestMemoryCacheTTL:
    """TTL 过期"""

    def test_expired_entry_returns_none(self):
        cache = MemoryCache(ttl=1)  # 1秒过期
        cache.set("ns1", "value", "key1")
        assert cache.get("ns1", "key1") == "value"
        time.sleep(1.1)
        assert cache.get("ns1", "key1") is None

    def test_not_expired_returns_value(self):
        cache = MemoryCache(ttl=10)
        cache.set("ns1", "value", "key1")
        time.sleep(0.1)
        assert cache.get("ns1", "key1") == "value"


class TestMemoryCacheClear:
    """clear 按命名空间清除"""

    def test_clear_specific_namespace(self):
        cache = MemoryCache(ttl=60)
        cache.set("ns1", "val1", "key1")
        cache.set("ns2", "val2", "key1")
        cache.clear("ns1")
        assert cache.get("ns1", "key1") is None
        assert cache.get("ns2", "key1") == "val2"  # ns2 不受影响

    def test_clear_all(self):
        cache = MemoryCache(ttl=60)
        cache.set("ns1", "val1", "key1")
        cache.set("ns2", "val2", "key1")
        cache.clear()
        assert cache.get("ns1", "key1") is None
        assert cache.get("ns2", "key1") is None

    def test_delete_pattern(self):
        """delete_pattern 清除整个命名空间"""
        cache = MemoryCache(ttl=60)
        cache.set("ns1", "val1", "key1")
        cache.set("ns1", "val2", "key2")
        cache.set("ns1", "val3", "key3")
        cache.delete_pattern("ns1", "*")
        assert cache.get("ns1", "key1") is None
        assert cache.get("ns1", "key2") is None
        assert cache.get("ns1", "key3") is None


class TestCacheKeyGeneration:
    """缓存键生成"""

    def test_same_args_same_key(self):
        """相同参数命中缓存"""
        cache = MemoryCache(ttl=60)
        cache.set("ns", "value", "a", "b", k="v")
        # 相同参数应命中
        assert cache.get("ns", "a", "b", k="v") == "value"

    def test_different_args_different_key(self):
        """不同参数不命中"""
        cache = MemoryCache(ttl=60)
        cache.set("ns", "value", "a", "b")
        assert cache.get("ns", "a", "c") is None  # 不同 args
        assert cache.get("ns", "a", "b", k="v") is None  # 多了 kwarg

    def test_kwargs_order_independent(self):
        """kwargs 顺序不影响缓存键"""
        cache = MemoryCache(ttl=60)
        cache.set("ns", "value", k1="v1", k2="v2")
        # 不同的 kwargs 顺序应命中同一缓存
        assert cache.get("ns", k2="v2", k1="v1") == "value"


class TestGetCacheSingleton:
    """get_cache 单例"""

    def test_returns_same_instance(self):
        reset_cache()
        c1 = get_cache()
        c2 = get_cache()
        assert c1 is c2

    def test_default_is_memory(self):
        """无 REDIS_URL 时返回 MemoryCache"""
        reset_cache()
        cache = get_cache()
        assert isinstance(cache, MemoryCache)

    def test_reset_clears_instance(self):
        reset_cache()
        c1 = get_cache()
        reset_cache()
        c2 = get_cache()
        assert c1 is not c2


class TestCacheWithComplexValues:
    """复杂值的缓存"""

    def test_cache_dict(self):
        cache = MemoryCache(ttl=60)
        data = {"title": "doc", "score": 0.85, "nested": {"a": 1}}
        cache.set("ns", data, "key1")
        assert cache.get("ns", "key1") == data

    def test_cache_list(self):
        cache = MemoryCache(ttl=60)
        data = [{"id": 1}, {"id": 2}]
        cache.set("ns", data, "key1")
        assert cache.get("ns", "key1") == data

    def test_cache_none_value(self):
        """缓存 None 值（注意：get 返回 None 可能是未命中也可能是缓存了 None）"""
        cache = MemoryCache(ttl=60)
        cache.set("ns", None, "key1")
        # get 返回 None，无法区分是未命中还是缓存了 None
        # 这是设计限制，实际使用时避免缓存 None
        assert cache.get("ns", "key1") is None
