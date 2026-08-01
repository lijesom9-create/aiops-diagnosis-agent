"""
API 限流模块

按用户 ID 限制请求频率，防止滥用。
- 滑动窗口算法：保留最近 window 秒内的请求时间戳，超过 max_requests 则拒绝
- 支持 Redis（分布式）和内存（单进程）两种后端
- 作为 FastAPI 依赖注入到需要限流的端点

使用方式：
    from .rate_limiter import rate_limit_dep

    @router.post("/chat/stream")
    async def chat_stream(..., _: None = Depends(rate_limit_dep)):
        ...
"""

import time
import threading
from collections import deque, defaultdict
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from loguru import logger

from .config import settings


# ========== 限流配置 ==========

# 每分钟最大请求数（按用户）
_DEFAULT_RPM = 30  # requests per minute
# 窗口大小（秒）
_WINDOW = 60


class RateLimiter:
    """滑动窗口限流器"""

    def __init__(self, max_requests: int = _DEFAULT_RPM, window: int = _WINDOW):
        self.max_requests = max_requests
        self.window = window
        # 内存模式：user_id -> deque[timestamps]
        self._requests: dict = defaultdict(deque)
        self._lock = threading.Lock()  # 保护内存计数器的并发访问
        # Redis 模式（延迟初始化）
        self._redis = None
        self._redis_ready = False

    def _init_redis(self):
        """尝试初始化 Redis 连接（用于分布式限流）"""
        redis_url = getattr(settings, "REDIS_URL", None)
        if not redis_url:
            return
        try:
            import redis
            self._redis = redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
            self._redis_ready = True
            logger.info("限流器使用 Redis 后端（分布式）")
        except Exception as e:
            logger.debug(f"Redis 不可用，限流降级到内存模式: {e}")
            self._redis = None
            self._redis_ready = False

    def check(self, user_id: str) -> bool:
        """检查用户是否超过请求限制

        Args:
            user_id: 用户 ID

        Returns:
            True 表示允许请求，False 表示被限流
        """
        if self._redis is None and not self._redis_ready and getattr(settings, "REDIS_URL", None):
            self._init_redis()

        if self._redis_ready:
            return self._check_redis(user_id)
        return self._check_memory(user_id)

    def _check_memory(self, user_id: str) -> bool:
        """内存滑动窗口（线程安全）"""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            dq = self._requests[user_id]

            # 清理过期时间戳
            while dq and dq[0] < cutoff:
                dq.popleft()

            if len(dq) >= self.max_requests:
                return False

            dq.append(now)
            return True

    def _check_redis(self, user_id: str) -> bool:
        """Redis 滑动窗口（ZSET 实现，分布式限流）"""
        import time as _time
        key = f"rag:rate_limit:{user_id}"
        now = _time.time()
        cutoff = now - self.window

        pipe = self._redis.pipeline()
        # 1. 移除过期成员
        pipe.zremrangebyscore(key, 0, cutoff)
        # 2. 统计当前窗口内请求数
        pipe.zcard(key)
        # 3. 如果未超限，添加当前请求
        pipe.zadd(key, {str(now): now})
        # 4. 设置 key 过期时间（避免内存泄漏）
        pipe.expire(key, self.window + 10)
        results = pipe.execute()

        current_count = results[1]
        return current_count < self.max_requests


# ========== 全局限流器实例 ==========

_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """获取限流器单例"""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(
            max_requests=getattr(settings, "RATE_LIMIT_RPM", _DEFAULT_RPM),
            window=_WINDOW,
        )
    return _rate_limiter


async def rate_limit_dep(request: Request):
    """FastAPI 依赖：按用户限流

    从请求中提取 user_id（JWT token 中的 sub），检查请求频率。
    超限时返回 429 Too Many Requests。

    用法：
        @router.post("/chat/stream")
        async def chat_stream(..., _: None = Depends(rate_limit_dep)):
    """
    # 从请求状态中获取 user_id（由 auth 中间件注入）
    # 如果未认证（如登录/注册接口），不限流
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        # 尝试从 cookie token 提取
        from .auth import get_user_id_from_request
        user_id = get_user_id_from_request(request)
        if not user_id:
            logger.debug("限流跳过：未获取到 user_id")
            return  # 未认证请求不限流

    limiter = get_rate_limiter()
    allowed = limiter.check(user_id)
    logger.debug(f"限流检查: user={user_id}, allowed={allowed}")
    if not allowed:
        logger.warning(f"用户 {user_id} 触发限流")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请求过于频繁，请 {_WINDOW} 秒后重试",
            headers={"Retry-After": str(_WINDOW)},
        )
