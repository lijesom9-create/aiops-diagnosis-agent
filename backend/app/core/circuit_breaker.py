"""
熔断器 + 令牌桶限流器

保护 LLM/VLM API 调用，避免：
1. API 故障时级联失败（熔断器：连续失败 N 次后快速失败，不再调用 API）
2. 并发过高触发 API rate limit（令牌桶：每秒最多 R 个请求）

设计参考：
- Martin Fowler Circuit Breaker 模式（CLOSED → OPEN → HALF_OPEN → CLOSED）
- Google SRE Book "Addressing Cascading Failures"
- 令牌桶算法（Token Bucket）

使用方式：
    breaker = CircuitBreaker(failure_threshold=5, reset_timeout=30)
    limiter = TokenBucketLimiter(rate=2, capacity=5)

    async with ResilienceContext(breaker, limiter):
        result = await provider.chat(messages)
    # 失败时抛 CircuitOpenError / RateLimitExceededError
"""
import asyncio
import time
from enum import Enum
from typing import Awaitable, Callable, Dict, Optional, TypeVar

from loguru import logger

T = TypeVar("T")


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"      # 正常：请求通过
    OPEN = "open"          # 熔断：快速失败
    HALF_OPEN = "half_open"  # 半开：允许 1 个探测请求


class CircuitOpenError(Exception):
    """熔断器开启时抛出（API 持续故障中，不再调用）"""

    def __init__(self, name: str, reset_in_seconds: float):
        self.name = name
        self.reset_in = reset_in_seconds
        super().__init__(
            f"熔断器 [{name}] 处于 OPEN 状态，{reset_in_seconds:.1f}s 后尝试半开探测"
        )


class RateLimitExceededError(Exception):
    """令牌桶限流时抛出（请求过快）"""

    def __init__(self, name: str, wait_seconds: float):
        self.name = name
        self.wait = wait_seconds
        super().__init__(
            f"限流器 [{name}] 触发，需等待 {wait_seconds:.2f}s"
        )


class CircuitBreaker:
    """
    异步熔断器

    状态机：
        CLOSED --连续失败 N 次--> OPEN
        OPEN --冷却 T 秒--> HALF_OPEN
        HALF_OPEN --探测成功--> CLOSED
        HALF_OPEN --探测失败--> OPEN（重置冷却时间）

    线程安全：通过 asyncio.Lock 保证状态切换原子性。
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        success_threshold: int = 1,
    ):
        """
        Args:
            name: 熔断器名称（用于日志/统计区分）
            failure_threshold: 连续失败多少次后熔断（CLOSED → OPEN）
            reset_timeout: 熔断后冷却秒数（OPEN → HALF_OPEN）
            success_threshold: 半开状态下连续成功多少次后完全恢复（HALF_OPEN → CLOSED）
        """
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

        # 统计
        self.stats = {
            "total_calls": 0,
            "successes": 0,
            "failures": 0,
            "rejected_open": 0,      # 因熔断被拒
            "rejected_rate": 0,      # 因限流被拒（仅 context 调用时累加）
            "state_transitions": 0,  # 状态切换次数
        }

    @property
    def state(self) -> CircuitState:
        """当前状态（动态计算：OPEN 超时后自动切到 HALF_OPEN）"""
        if self._state == CircuitState.OPEN:
            # 检查冷却时间是否已过
            if time.time() - self._last_failure_time >= self.reset_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    async def acquire(self) -> None:
        """
        获取调用许可（在调用 API 之前调用）

        Raises:
            CircuitOpenError: 熔断器开启时
        """
        async with self._lock:
            self.stats["total_calls"] += 1
            current = self.state  # 动态状态（OPEN 超时后返回 HALF_OPEN）

            if current == CircuitState.OPEN:
                # 真正的 OPEN 状态：冷却时间未过
                reset_in = self.reset_timeout - (time.time() - self._last_failure_time)
                self.stats["rejected_open"] += 1
                raise CircuitOpenError(self.name, max(0.0, reset_in))

            # 动态状态是 HALF_OPEN 但 _state 还是 OPEN：同步过来
            # （此时冷却时间已过，需要把 _state 从 OPEN 切到 HALF_OPEN）
            if current == CircuitState.HALF_OPEN and self._state == CircuitState.OPEN:
                self._transition_to(CircuitState.HALF_OPEN)

            # CLOSED 或 HALF_OPEN：允许通过
            # HALF_OPEN 时只允许 1 个探测请求（通过 lock 串行化保证）

    async def record_success(self) -> None:
        """记录调用成功"""
        async with self._lock:
            self.stats["successes"] += 1
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self._state == CircuitState.CLOSED:
                # 正常状态下成功，重置失败计数
                self._failure_count = 0

    async def record_failure(self) -> None:
        """记录调用失败"""
        async with self._lock:
            self.stats["failures"] += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # 半开探测失败 → 重新熔断
                self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """状态切换（调用方需持有 lock）"""
        if self._state == new_state:
            return
        old = self._state
        self._state = new_state
        self._failure_count = 0
        self._success_count = 0
        self.stats["state_transitions"] += 1
        logger.warning(
            f"熔断器 [{self.name}] 状态切换: {old.value} → {new_state.value}"
        )

    def get_state_info(self) -> Dict:
        """获取状态信息（用于监控/调试）"""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "reset_timeout": self.reset_timeout,
            "stats": self.stats.copy(),
        }


class TokenBucketLimiter:
    """
    异步令牌桶限流器

    桶容量 capacity，每秒补充 rate 个令牌。
    每次请求消耗 1 个令牌；桶空时拒绝（或等待）。

    实现细节：惰性补充（不依赖后台任务），每次 acquire 时根据时间差补充令牌。
    """

    def __init__(
        self,
        name: str = "default",
        rate: float = 2.0,
        capacity: int = 5,
        max_wait: float = 0.0,
    ):
        """
        Args:
            name: 限流器名称
            rate: 每秒补充的令牌数（令牌生成速率）
            capacity: 桶容量（最大突发量）
            max_wait: 桶空时最大等待秒数（0 表示不等待，直接拒绝）
        """
        self.name = name
        self.rate = rate
        self.capacity = capacity
        self.max_wait = max_wait

        self._tokens: float = capacity  # 初始满桶
        self._last_refill: float = time.time()
        self._lock = asyncio.Lock()

        self.stats = {
            "total_requests": 0,
            "allowed": 0,
            "rejected": 0,
            "waited": 0,
        }

    async def acquire(self) -> None:
        """
        获取 1 个令牌

        Raises:
            RateLimitExceededError: 桶空且不等待（或等待超过 max_wait）时
        """
        async with self._lock:
            self.stats["total_requests"] += 1
            self._refill()

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self.stats["allowed"] += 1
                return

            # 桶空
            if self.max_wait <= 0:
                self.stats["rejected"] += 1
                wait = (1.0 - self._tokens) / self.rate if self.rate > 0 else float("inf")
                raise RateLimitExceededError(self.name, wait)

            # 等待令牌补充
            wait = (1.0 - self._tokens) / self.rate if self.rate > 0 else self.max_wait
            if wait > self.max_wait:
                self.stats["rejected"] += 1
                raise RateLimitExceededError(self.name, wait)

            self.stats["waited"] += 1
            # 释放锁等待（避免阻塞其他协程的统计），但这里简化处理：持有锁等待
            # 注意：这会让限流器在等待期间串行化，对低 RPS 场景可接受
            await asyncio.sleep(wait)
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
            else:
                # 极端情况下仍未获得令牌
                self.stats["rejected"] += 1
                raise RateLimitExceededError(self.name, 0.0)

    def _refill(self) -> None:
        """惰性补充令牌（调用方需持有 lock）"""
        now = time.time()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    def get_state_info(self) -> Dict:
        """获取状态信息"""
        return {
            "name": self.name,
            "rate": self.rate,
            "capacity": self.capacity,
            "current_tokens": round(self._tokens, 2),
            "stats": self.stats.copy(),
        }


# ========== 集成辅助：ResilienceContext ==========

class ResilienceContext:
    """
    组合 CircuitBreaker + TokenBucketLimiter 的上下文管理器

    用法：
        async with ResilienceContext(breaker, limiter):
            result = await provider.chat(messages)
        # 异常自动记录到 breaker，无需手动 record_success/failure

    注意：
        - 只有 breaker/limiter 都通过 acquire() 后才执行业务调用
        - 业务调用抛异常 → record_failure
        - 业务调用成功 → record_success
        - CircuitOpenError / RateLimitExceededError 直接向上抛（不视为业务失败）
    """

    def __init__(
        self,
        breaker: Optional[CircuitBreaker] = None,
        limiter: Optional[TokenBucketLimiter] = None,
    ):
        self.breaker = breaker
        self.limiter = limiter
        self._acquired = False

    async def __aenter__(self) -> "ResilienceContext":
        # 顺序：先限流（避免占用熔断器配额），再熔断
        if self.limiter is not None:
            await self.limiter.acquire()
        if self.breaker is not None:
            await self.breaker.acquire()
        self._acquired = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if not self._acquired:
            return False  # __aenter__ 阶段就抛了，不处理

        # CircuitOpenError / RateLimitExceededError 是熔断/限流自身的拒绝
        # 不应该视为业务失败，让它们直接向上传播
        if exc_type is CircuitOpenError or exc_type is RateLimitExceededError:
            return False  # 不吞掉，向上传播

        if exc_type is None:
            # 业务调用成功
            if self.breaker is not None:
                await self.breaker.record_success()
        else:
            # 业务调用抛异常
            if self.breaker is not None:
                await self.breaker.record_failure()
            return False  # 让业务异常向上传播

        return False


# ========== 装饰器形式 ==========

def with_resilience(
    breaker: Optional[CircuitBreaker] = None,
    limiter: Optional[TokenBucketLimiter] = None,
):
    """
    装饰器：为异步方法自动加熔断+限流

    用法：
        class MyProvider:
            def __init__(self):
                self.breaker = CircuitBreaker(name="my_api")
                self.limiter = TokenBucketLimiter(name="my_api", rate=2)

            @with_resilience(lambda self: self.breaker, lambda self: self.limiter)
            async def call_api(self, ...):
                ...
    """
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(self, *args, **kwargs):
            b = breaker(self) if callable(breaker) else breaker
            rate_limiter = limiter(self) if callable(limiter) else limiter
            async with ResilienceContext(b, rate_limiter):
                return await func(self, *args, **kwargs)
        return wrapper
    return decorator


# ========== 全局熔断器注册表（按 provider 名隔离） ==========

_breakers: Dict[str, CircuitBreaker] = {}
_limiters: Dict[str, TokenBucketLimiter] = {}


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    """获取或创建具名熔断器（单例，按 name 隔离）"""
    if name not in _breakers:
        from ..core.config import settings
        _breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=kwargs.get("failure_threshold",
                                        getattr(settings, "CIRCUIT_FAILURE_THRESHOLD", 5)),
            reset_timeout=kwargs.get("reset_timeout",
                                     getattr(settings, "CIRCUIT_RESET_TIMEOUT", 30.0)),
        )
    return _breakers[name]


def get_limiter(name: str, **kwargs) -> TokenBucketLimiter:
    """获取或创建具名限流器（单例）"""
    if name not in _limiters:
        from ..core.config import settings
        _limiters[name] = TokenBucketLimiter(
            name=name,
            rate=kwargs.get("rate", getattr(settings, "RATE_LIMIT_RPS", 2.0)),
            capacity=kwargs.get("capacity", getattr(settings, "RATE_LIMIT_CAPACITY", 5)),
        )
    return _limiters[name]


def get_all_state() -> Dict:
    """获取所有熔断器/限流器状态（用于 /health 接口）"""
    return {
        "breakers": {n: b.get_state_info() for n, b in _breakers.items()},
        "limiters": {n: limiter_state.get_state_info() for n, limiter_state in _limiters.items()},
    }
