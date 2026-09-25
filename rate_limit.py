"""
轻量进程内限流器。

用于保护公开端点（CDK 兑换、续费、子域名查重、日志解析）与管理员登录，
防止单 IP 高频请求打满线程池或暴力破解口令。

设计取舍：
- 纯内存滑动窗口，无第三方依赖；单进程部署足够，多 worker 时按 worker 独立计数（可接受）。
- 只保存 (时间戳队列)，超过窗口的旧记录会惰性清理，避免无界增长。
"""

import threading
import time
from typing import Dict, Deque, Tuple
from collections import deque

# 触发限流后返回给前端的统一提示
RATE_LIMIT_MESSAGE = "操作过于频繁，请稍后再试"

_LOCK = threading.Lock()
_BUCKETS: Dict[str, Deque[float]] = {}

# 桶数量上限，防止恶意构造大量 key 造成内存膨胀
_MAX_BUCKETS = 20000


def _prune_dead_buckets(now_ts: float, window: float) -> None:
    if len(_BUCKETS) <= _MAX_BUCKETS:
        return
    for key in list(_BUCKETS.keys()):
        queue = _BUCKETS.get(key)
        if not queue or (now_ts - queue[-1]) > window:
            _BUCKETS.pop(key, None)
        if len(_BUCKETS) <= _MAX_BUCKETS // 2:
            break


def check_rate_limit(key: str, limit: int, window_seconds: float) -> Tuple[bool, int]:
    """
    记录一次访问并判定是否超限。

    返回: (allowed, retry_after_seconds)
    """
    if limit <= 0:
        return True, 0
    now_ts = time.time()
    with _LOCK:
        queue = _BUCKETS.get(key)
        if queue is None:
            queue = deque()
            _BUCKETS[key] = queue
        # 清理窗口外的旧记录
        while queue and (now_ts - queue[0]) > window_seconds:
            queue.popleft()
        if len(queue) >= limit:
            retry_after = max(1, int(window_seconds - (now_ts - queue[0])) + 1)
            return False, retry_after
        queue.append(now_ts)
        _prune_dead_buckets(now_ts, window_seconds)
        return True, 0


def reset_rate_limit(key_prefix: str = "") -> None:
    """清空限流状态（测试与运维使用）。"""
    with _LOCK:
        if not key_prefix:
            _BUCKETS.clear()
            return
        for key in list(_BUCKETS.keys()):
            if key.startswith(key_prefix):
                _BUCKETS.pop(key, None)


class RateLimiter:
    """便捷封装：一个 limiter 实例对应一类端点，按 IP 维度计数。"""

    def __init__(self, name: str, limit: int, window_seconds: float):
        self.name = name
        self.limit = limit
        self.window_seconds = window_seconds

    def hit(self, client_ip: str) -> Tuple[bool, int]:
        return check_rate_limit(
            f"{self.name}:{client_ip or 'unknown'}",
            self.limit,
            self.window_seconds,
        )


# 各端点预设配额
LOGIN_LIMITER = RateLimiter("admin-login", limit=8, window_seconds=60)
REDEEM_LIMITER = RateLimiter("redeem", limit=20, window_seconds=60)
PUBLIC_LOOKUP_LIMITER = RateLimiter("public-lookup", limit=60, window_seconds=60)
PARSE_LOG_LIMITER = RateLimiter("parse-log", limit=20, window_seconds=60)


def rate_limit_guard(limiter: RateLimiter, client_ip: str) -> bool:
    """命中限流时返回 False，调用方据此返回 429。"""
    allowed, _retry = limiter.hit(client_ip)
    return allowed