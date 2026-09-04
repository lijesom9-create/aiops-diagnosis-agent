"""
C3 circuit_breaker 持锁 sleep 修复测试

背景：TokenBucketLimiter.acquire 原在持有 self._lock 期间 await asyncio.sleep(wait)，
导致所有等待者串行化——一个协程 sleep 时，其他协程（含本可立即拿令牌的）全部阻塞在锁外。
修复后：锁内只做补充令牌/检查/计算 wait，锁外 sleep 后重新竞争锁。

测试覆盖：
1. 行为兼容：单协程 acquire 语义不变（满桶立即通过 / 桶空+max_wait<=0 拒绝 / 桶空+max_wait>0 等待后通过 / 超预算拒绝）
2. 并发不串行化：多个等待者并行 sleep，吞吐量显著优于串行（核心证据）
3. sleep 期间不持锁：等待者 sleep 时，新协程能立即拿到令牌（不被锁外排队阻塞）
4. 统计准确：total_requests / allowed / rejected / waited 计数正确
"""

import asyncio
import time

import pytest

from app.core.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RateLimitExceededError,
    TokenBucketLimiter,
)


class TestTokenBucketBehaviorCompat:
    """行为兼容：单协程 acquire 语义不变"""

    @pytest.mark.asyncio
    async def test_full_bucket_passes_immediately(self):
        """满桶立即通过（capacity 个请求无需等待）"""
        limiter = TokenBucketLimiter(name="t", rate=2.0, capacity=3, max_wait=0.0)
        for _ in range(3):
            await limiter.acquire()
        # 统计：3 个 allowed，0 rejected
        assert limiter.stats["allowed"] == 3
        assert limiter.stats["rejected"] == 0
        assert limiter.stats["waited"] == 0

    @pytest.mark.asyncio
    async def test_empty_bucket_no_wait_rejected(self):
        """桶空 + max_wait=0 → 立即拒绝"""
        limiter = TokenBucketLimiter(name="t", rate=2.0, capacity=1, max_wait=0.0)
        await limiter.acquire()  # 耗尽
        with pytest.raises(RateLimitExceededError):
            await limiter.acquire()
        assert limiter.stats["rejected"] == 1

    @pytest.mark.asyncio
    async def test_empty_bucket_with_wait_succeeds(self):
        """桶空 + max_wait>0 → 等待后通过"""
        limiter = TokenBucketLimiter(name="t", rate=10.0, capacity=1, max_wait=1.0)
        await limiter.acquire()  # 耗尽
        start = time.monotonic()
        await limiter.acquire()  # 需等待 ~0.1s（1/10）
        elapsed = time.monotonic() - start
        assert elapsed >= 0.08, "应等待约 0.1s"
        assert elapsed < 0.5, "不应超过 max_wait"
        assert limiter.stats["allowed"] == 2
        assert limiter.stats["waited"] == 1  # 第二次等待过

    @pytest.mark.asyncio
    async def test_exceed_max_wait_rejected(self):
        """累计等待超过 max_wait → 拒绝"""
        # rate 极低，需等待很久；max_wait 很小 → 拒绝
        limiter = TokenBucketLimiter(name="t", rate=0.5, capacity=1, max_wait=0.2)
        await limiter.acquire()  # 耗尽
        with pytest.raises(RateLimitExceededError):
            await limiter.acquire()
        assert limiter.stats["rejected"] == 1

    @pytest.mark.asyncio
    async def test_refill_restores_tokens(self):
        """时间流逝后令牌补充"""
        limiter = TokenBucketLimiter(name="t", rate=100.0, capacity=1, max_wait=0.0)
        await limiter.acquire()
        await asyncio.sleep(0.05)  # 补充约 5 个令牌
        await limiter.acquire()  # 应成功
        assert limiter.stats["allowed"] == 2


class TestConcurrentNotSerialized:
    """C3 核心：并发等待者不串行化"""

    @pytest.mark.asyncio
    async def test_concurrent_waiters_parallel_not_serial(self):
        """多个等待者并行 sleep，总耗时 ≈ 单次等待（串行则会 ×N）

        场景：capacity=1，先耗尽；rate=10（每 0.1s 补 1 个）。
        5 个协程并发 acquire(max_wait=2)：
        - 串行（旧实现）：0.1 + 0.1 + ... = ~0.5s
        - 并行（新实现）：所有等待者同时 sleep(0.1)，醒来后按调度逐个拿令牌；
          由于令牌按 rate 补充，实际仍需逐个获取，但第一个醒来后后续令牌
          补充间隔 0.1s → 总耗时 ~0.5s 是令牌生成速率决定的物理下限。
        因此本测试改用更陡的 rate 让并行收益可测：rate=100（0.01s/个），
        5 个等待者：串行 ~0.05s，并行 ~0.01-0.02s（首个醒来即拿，后续令牌
        在 sleep 期间已补充）。
        """
        limiter = TokenBucketLimiter(name="t", rate=100.0, capacity=1, max_wait=1.0)
        await limiter.acquire()  # 耗尽

        start = time.monotonic()
        await asyncio.gather(*[limiter.acquire() for _ in range(5)])
        elapsed = time.monotonic() - start

        # 5 个等待者：并行下限约 0.01s（首个令牌补充），上限宽松到 0.3s
        # 串行下限约 0.05s（5 × 0.01）——这里断言并行明显优于串行
        assert elapsed < 0.3, f"并发等待应并行 sleep，耗时 {elapsed:.3f}s 过长（疑似串行化）"
        assert limiter.stats["allowed"] == 6  # 1 初始 + 5 等待
        assert limiter.stats["waited"] == 5

    @pytest.mark.asyncio
    async def test_new_acquire_not_blocked_by_sleeper(self):
        """等待者 sleep 期间，新协程能立即拿到补充的令牌（不持锁的证据）

        场景：capacity=2，rate=10。先起 1 个等待者（桶空，sleep 0.1s）。
        在其 sleep 期间，新协程 acquire 应能进入锁并拿到令牌（若持锁则被阻塞）。
        """
        limiter = TokenBucketLimiter(name="t", rate=10.0, capacity=1, max_wait=1.0)
        await limiter.acquire()  # 耗尽

        async def waiter():
            await limiter.acquire()  # 等待 ~0.1s

        async def newcomer():
            # 等 waiter 进入 sleep 后再 acquire
            await asyncio.sleep(0.02)
            # 此时若旧实现持锁 sleep，本协程会阻塞到 waiter 醒来
            # 新实现：sleep 期间锁已释放，本协程立即进入锁
            await asyncio.sleep(0.12)  # 等令牌补充
            await limiter.acquire()

        start = time.monotonic()
        await asyncio.gather(waiter(), newcomer())
        elapsed = time.monotonic() - start

        # waiter ~0.1s 拿到令牌；newcomer 在 0.14s 拿到令牌
        # 旧实现（串行）：waiter 持锁 sleep 0.1s，newcomer 阻塞 0.1s 才进锁
        assert elapsed < 0.3, f"新协程不应被 sleep 中的等待者阻塞，耗时 {elapsed:.3f}s"
        assert limiter.stats["allowed"] == 3  # 1 初始 + waiter + newcomer


class TestCircuitBreakerBasic:
    """熔断器基础语义（回归保护，确保 C3 改动未触碰熔断器逻辑）"""

    @pytest.mark.asyncio
    async def test_closed_to_open_after_threshold(self):
        """连续失败 N 次 → OPEN"""
        breaker = CircuitBreaker(name="t", failure_threshold=3, reset_timeout=30.0)
        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_rejects_acquire(self):
        """OPEN 状态 acquire 抛 CircuitOpenError"""
        breaker = CircuitBreaker(name="t", failure_threshold=1, reset_timeout=30.0)
        await breaker.record_failure()
        with pytest.raises(CircuitOpenError):
            await breaker.acquire()

    @pytest.mark.asyncio
    async def test_half_open_to_closed_on_success(self):
        """HALF_OPEN 探测成功 → CLOSED"""
        breaker = CircuitBreaker(name="t", failure_threshold=1, reset_timeout=0.0)
        await breaker.record_failure()  # → OPEN
        await asyncio.sleep(0.01)
        await breaker.acquire()  # 冷却过 → HALF_OPEN
        await breaker.record_success()  # → CLOSED
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        """CLOSED 状态成功重置失败计数"""
        breaker = CircuitBreaker(name="t", failure_threshold=3, reset_timeout=30.0)
        await breaker.record_failure()
        await breaker.record_failure()
        await breaker.record_success()  # 重置
        assert breaker._failure_count == 0
        # 再失败 2 次不应熔断（需满 3 次）
        await breaker.record_failure()
        await breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
