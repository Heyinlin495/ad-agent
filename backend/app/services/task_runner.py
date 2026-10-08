"""后台任务执行器。

用独立线程池承载广告生成任务，与 Web 请求线程隔离并限制并发上限，
避免长耗时任务（LLM / 图像生成，可达数分钟）挤占请求处理能力。

范围说明：仍为**进程内**执行，进程重启会丢失在途任务，由启动时的
`task_service.recover_orphan_tasks` 兜底标记为失败。若需要跨进程重启恢复，
把 `submit` 的实现替换为 Celery / Redis 投递即可，调用方无需改动。

背压：ThreadPoolExecutor 的待办队列本身无上限，海量提交会让内存无上限增长
（每个待办任务都持有闭包与图片字节）。因此这里自行统计「在途任务数」，
超过上限直接抛 429，让调用方明确感知而不是把进程内存撑爆。
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any

from loguru import logger

from app.core.config import settings


class QueueFullError(RuntimeError):
    """任务队列已满（在途任务数达到上限）。"""


_MAX_WORKERS = max(settings.task_max_workers, 1)
# 在途上限：未显式配置时给工作线程数的 2 倍排队深度
_configured_inflight = int(settings.task_max_inflight or 0)
_MAX_INFLIGHT = max(_configured_inflight or _MAX_WORKERS * 2, _MAX_WORKERS)

_executor = ThreadPoolExecutor(
    max_workers=_MAX_WORKERS,
    thread_name_prefix="ad-task",
)

_inflight = 0
_inflight_lock = Lock()


def inflight_count() -> int:
    """当前在途（排队 + 执行中）的任务数。"""
    with _inflight_lock:
        return _inflight


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """提交后台任务（立即返回，不阻塞请求）。

    队列已满时抛 QueueFullError，由调用方转成 429。
    """
    global _inflight
    with _inflight_lock:
        if _inflight >= _MAX_INFLIGHT:
            raise QueueFullError(
                f"任务队列已满（在途 {_inflight}/{_MAX_INFLIGHT}），请稍后重试"
            )
        _inflight += 1
    try:
        _executor.submit(_safe_run, fn, *args, **kwargs)
    except BaseException:
        # 提交失败（如解释器关闭）需归还计数，避免计数泄漏导致永久拒绝
        with _inflight_lock:
            _inflight -= 1
        raise


def _safe_run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    global _inflight
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001  兜底记录，避免线程池静默吞掉异常
        logger.exception(f"后台任务 {getattr(fn, '__name__', fn)} 执行异常")
    finally:
        with _inflight_lock:
            _inflight = max(0, _inflight - 1)


def shutdown(wait: bool = False) -> None:
    """关闭线程池（应用退出时调用）。"""
    _executor.shutdown(wait=wait)
