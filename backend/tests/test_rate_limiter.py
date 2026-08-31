"""
API 限流器单元测试

测试覆盖：
- 基本限流（允许 max_requests 次，第 max_requests+1 次拒绝）
- 不同用户独立计数
- 时间窗口过期后恢复
- 并发安全（多线程同时请求）
- Redis 降级到内存模式
"""

import threading
import time

from app.core.rate_limiter import RateLimiter


class TestBasicRateLimiting:
    """基本限流逻辑"""

    def test_allows_up_to_limit(self):
        """允许 max_requests 次请求"""
        limiter = RateLimiter(max_requests=5, window=60)
        results = [limiter.check("user1") for _ in range(5)]
        assert all(results), "前5次请求应全部允许"

    def test_blocks_over_limit(self):
        """超过 max_requests 次后拒绝"""
        limiter = RateLimiter(max_requests=3, window=60)
        for _ in range(3):
            assert limiter.check("user1") is True
        # 第4次应被拒绝
        assert limiter.check("user1") is False

    def test_blocks_multiple_over_limit(self):
        """连续超限都拒绝"""
        limiter = RateLimiter(max_requests=2, window=60)
        limiter.check("user1")
        limiter.check("user1")
        for _ in range(10):
            assert limiter.check("user1") is False, "超限后应持续拒绝"


class TestUserIsolation:
    """不同用户独立计数"""

    def test_different_users_independent(self):
        """用户 A 超限不影响用户 B"""
        limiter = RateLimiter(max_requests=2, window=60)
        # 用户 A 耗尽配额
        limiter.check("userA")
        limiter.check("userA")
        assert limiter.check("userA") is False
        # 用户 B 不受影响
        assert limiter.check("userB") is True
        assert limiter.check("userB") is True
        assert limiter.check("userB") is False  # B 也到上限

    def test_none_user_not_limited(self):
        """user_id 为 None 时不限流（实际由调用方处理）"""
        limiter = RateLimiter(max_requests=1, window=60)
        # check(None) 也能工作，用 None 作为 key
        limiter.check(None)
        # 不会崩溃
        assert isinstance(limiter.check(None), bool)


class TestWindowExpiry:
    """时间窗口过期"""

    def test_window_expiry_restores_access(self):
        """窗口过期后恢复访问"""
        limiter = RateLimiter(max_requests=2, window=1)  # 1秒窗口
        limiter.check("user1")
        limiter.check("user1")
        assert limiter.check("user1") is False  # 被限流

        time.sleep(1.1)  # 等待窗口过期
        assert limiter.check("user1") is True   # 恢复访问

    def test_partial_window_expiry(self):
        """部分时间戳过期，部分仍在窗口内"""
        limiter = RateLimiter(max_requests=3, window=1)
        limiter.check("user1")  # t=0
        time.sleep(0.6)
        limiter.check("user1")  # t=0.6
        time.sleep(0.6)  # t=1.2，第一个请求过期
        # 第一个请求已过期，但第二个还在窗口内
        assert limiter.check("user1") is True   # 允许（窗口内只有1个）
        assert limiter.check("user1") is True   # 允许（窗口内有2个）
        assert limiter.check("user1") is False  # 拒绝（窗口内有3个，到上限）


class TestConcurrency:
    """并发安全"""

    def test_concurrent_requests_respect_limit(self):
        """35个并发请求，max=30，应拦截5个"""
        limiter = RateLimiter(max_requests=30, window=60)
        results = [None] * 35

        def check_thread(i):
            results[i] = limiter.check("user1")

        threads = [threading.Thread(target=check_thread, args=(i,)) for i in range(35)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed = sum(1 for r in results if r is True)
        blocked = sum(1 for r in results if r is False)
        assert allowed == 30, f"应允许30次，实际{allowed}"
        assert blocked == 5, f"应拦截5次，实际{blocked}"


class TestRedisFallback:
    """Redis 降级（不依赖真实 Redis 服务）"""

    def test_memory_mode_when_no_redis(self):
        """无 REDIS_URL 时使用内存模式"""
        limiter = RateLimiter(max_requests=3, window=60)
        # _redis_ready 应为 False（未配置 Redis）
        assert limiter._redis_ready is False
        assert limiter._redis is None
        # 内存模式正常工作
        for _ in range(3):
            assert limiter.check("user1") is True
        assert limiter.check("user1") is False
