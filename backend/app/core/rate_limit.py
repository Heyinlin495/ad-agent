"""基础限流（内存版）。

生产环境建议替换为 Redis 滑动窗口限流，此处提供无外部依赖的默认实现。
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import AppError


class TooManyRequestsError(AppError):
    """请求过于频繁。"""

    code = 42900
    http_status = 429
    message = "Too many requests, please try again later"


class RateLimiter:
    """基于内存的滑动窗口限流器（线程安全 + key 数量有界）。

    设计约束：
    - **线程安全**：FastAPI 的接口依赖会在线程池中并发执行，所有对 `_hits` 的
      读改写都必须持锁，否则 deque 的 `popleft` / `append`、dict 的增删会在并发
      下互相破坏（计数错乱）。锁内操作极轻量（仅 deque/dict），可忽略；
      `_sweep` 的 O(n) 扫描也在锁内，但受 `_ops_since_sweep` 频控。
    - **key 数量上界**：`_hits` 的 key 来自客户端 IP（外部可控），不加清理会让
      dict 无上限增长形成内存泄漏。达到 `max_keys` 时强制淘汰空桶 / 最老的 key。
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int = 60,
        max_keys: int = 10000,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = 0.0
        # 简单计数，避免每次请求都 O(n) 扫描
        self._ops_since_sweep = 0

    def _sweep(self, now: float) -> None:
        """清理过期桶；仍超限时按插入顺序淘汰最老的 key（调用方需持有 _lock）。"""
        expired = [
            k for k, q in self._hits.items() if not q or now - q[-1] > self.window_seconds
        ]
        for k in expired:
            self._hits.pop(k, None)
        if len(self._hits) > self.max_keys:
            # dict 保持插入序，从最老的开始淘汰到上限
            overflow = len(self._hits) - self.max_keys
            for k in list(self._hits.keys())[:overflow]:
                self._hits.pop(k, None)
        self._last_sweep = now
        self._ops_since_sweep = 0

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window_seconds:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            self._ops_since_sweep += 1
            if len(self._hits) > self.max_keys or self._ops_since_sweep >= 512:
                self._sweep(now)
            return True


_limiter = RateLimiter(max_requests=settings.rate_limit_per_minute)
# 重接口（调用 LLM / 视觉识别 / 图像模型 / 抠图，成本高）使用更严格的配额
_heavy_limiter = RateLimiter(max_requests=max(settings.rate_limit_per_minute // 6, 5))


def _real_client_ip(request: Request) -> str:
    """提取真实客户端 IP。

    反向代理（nginx / 网关）之后 `request.client.host` 是代理地址，所有上游用户
    会共享一个 token bucket，导致限流形同虚设且互相误伤。优先读
    `X-Forwarded-For`（取**最左**，即最先由源头客户端填入的原始地址）/
    `X-Real-IP`；取不到再回退直连地址。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def client_key(request: Request) -> str:
    return _real_client_ip(request)


def check_rate_limit(request: Request) -> bool:
    return _limiter.is_allowed(client_key(request))


def rate_limit(request: Request) -> None:
    """FastAPI 依赖：普通接口限流。超限抛 429。"""
    if not _limiter.is_allowed(client_key(request)):
        raise TooManyRequestsError()


def heavy_rate_limit(request: Request) -> None:
    """FastAPI 依赖：重接口限流。超限抛 429。"""
    if not _heavy_limiter.is_allowed(client_key(request)):
        raise TooManyRequestsError()