"""轻量进程内运行指标（线程安全）。

用于在没有 Prometheus 等外部依赖的情况下，暴露最关键的运行态数据：
任务成功/失败数、平均耗时、LLM 调用次数与平均延迟等。多进程部署时各进程
指标相互独立（如需聚合请改用外部 TSDB / Prometheus）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

_lock = threading.Lock()
_started_at = time.time()

_counters: dict[str, int] = defaultdict(int)
_llm_calls: dict[str, int] = defaultdict(int)          # purpose -> 次数
_llm_latency_sum: dict[str, int] = defaultdict(int)    # purpose -> 累计延迟(ms)
_llm_tokens_in: dict[str, int] = defaultdict(int)
_llm_tokens_out: dict[str, int] = defaultdict(int)
_llm_cost: float = 0.0
_task_status: dict[str, int] = defaultdict(int)        # status -> 次数
_task_duration_sum: dict[str, int] = defaultdict(int)  # status -> 累计耗时(ms)


def incr(name: str, value: int = 1) -> None:
    """通用计数器自增。"""
    with _lock:
        _counters[name] += value


def record_llm_call(
    purpose: str,
    latency_ms: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
) -> None:
    """记录一次 LLM 调用。"""
    key = purpose or "unknown"
    global _llm_cost
    with _lock:
        _llm_calls[key] += 1
        _llm_latency_sum[key] += max(0, int(latency_ms))
        _llm_tokens_in[key] += max(0, int(tokens_in))
        _llm_tokens_out[key] += max(0, int(tokens_out))
        _llm_cost += max(0.0, float(cost))


def record_task(status: str, duration_ms: int) -> None:
    """记录一个任务的终态与耗时。"""
    key = status or "unknown"
    with _lock:
        _task_status[key] += 1
        _task_duration_sum[key] += max(0, int(duration_ms))


def reset() -> None:
    """清空所有指标（测试用）。"""
    global _llm_cost, _started_at
    with _lock:
        _counters.clear()
        _llm_calls.clear()
        _llm_latency_sum.clear()
        _llm_tokens_in.clear()
        _llm_tokens_out.clear()
        _task_status.clear()
        _task_duration_sum.clear()
        _llm_cost = 0.0
        _started_at = time.time()


def prometheus_text() -> str:
    """导出 Prometheus 文本格式（text/plain; version=0.0.4；带 {type}）指标。

    供 /metrics/prometheus 端点使用：可被 Prometheus 抓取并接入 Grafana。
    与 snapshot() 共享同一份进程内数据（多 worker 时各进程独立，如需聚合由
    Prometheus 拉取多副本后自行 merge）。
    """
    with _lock:
        counters = dict(_counters)
        llm_calls = dict(_llm_calls)
        llm_latency = dict(_llm_latency_sum)
        tokens_in = dict(_llm_tokens_in)
        tokens_out = dict(_llm_tokens_out)
        task_status = dict(_task_status)
        task_duration = dict(_task_duration_sum)
        llm_cost = _llm_cost
        started = _started_at

    lines: list[str] = []
    # 只导出 TIME 与 GAUGE/COUNTER；# HELP / # TYPE 帮助文本便于 Grafana 自动识别

    def _help_and_type(name: str, doc: str, typ: str) -> None:
        lines.append(f"# HELP {name} {doc}")
        lines.append(f"# TYPE {name} {typ}")

    _help_and_type("ad_agent_uptime_seconds", "Process uptime in seconds", "gauge")
    lines.append(f"ad_agent_uptime_seconds {time.time() - started:.1f}")

    _help_and_type("ad_agent_llm_calls_total", "LLM calls by purpose", "counter")
    for k, v in sorted(llm_calls.items()):
        lines.append(f'ad_agent_llm_calls_total{{purpose="{_esc(k)}"}} {v}')

    _help_and_type("ad_agent_llm_latency_seconds_sum", "Cumulative LLM latency seconds by purpose", "counter")
    for k, v in sorted(llm_latency.items()):
        lines.append(f'ad_agent_llm_latency_seconds_sum{{purpose="{_esc(k)}"}} {v / 1000.0:.3f}')

    _help_and_type("ad_agent_llm_tokens_in_total", "Input tokens by purpose", "counter")
    for k, v in sorted(tokens_in.items()):
        lines.append(f'ad_agent_llm_tokens_in_total{{purpose="{_esc(k)}"}} {v}')

    _help_and_type("ad_agent_llm_tokens_out_total", "Output tokens by purpose", "counter")
    for k, v in sorted(tokens_out.items()):
        lines.append(f'ad_agent_llm_tokens_out_total{{purpose="{_esc(k)}"}} {v}')

    _help_and_type("ad_agent_llm_cost_usd_total", "Estimated LLM cost in USD", "counter")
    lines.append(f"ad_agent_llm_cost_usd_total {llm_cost:.6f}")

    _help_and_type("ad_agent_tasks_total", "Task terminal count by status", "counter")
    for k, v in sorted(task_status.items()):
        lines.append(f'ad_agent_tasks_total{{status="{_esc(k)}"}} {v}')

    _help_and_type("ad_agent_task_duration_seconds_sum", "Cumulative task duration seconds by status", "counter")
    for k, v in sorted(task_duration.items()):
        lines.append(f'ad_agent_task_duration_seconds_sum{{status="{_esc(k)}"}} {v / 1000.0:.3f}')

    _help_and_type("ad_agent_counters", "Generic counters", "untyped")
    for k, v in sorted(counters.items()):
        lines.append(f'ad_agent_counters{{name="{_esc(k)}"}} {v}')

    return "\n".join(lines) + "\n"


def _esc(label: str) -> str:
    """转义 Prometheus 标签值（反斜杠与引号）。"""
    return label.replace("\\", "\\\\").replace('"', '\\"')


def snapshot() -> dict[str, Any]:
    """导出当前指标快照。"""
    with _lock:
        counters = dict(_counters)
        llm_calls = dict(_llm_calls)
        llm_latency = dict(_llm_latency_sum)
        tokens_in = dict(_llm_tokens_in)
        tokens_out = dict(_llm_tokens_out)
        task_status = dict(_task_status)
        task_duration = dict(_task_duration_sum)
        llm_cost = _llm_cost
        started = _started_at

    llm_total = sum(llm_calls.values())
    llm_by_purpose = {
        k: {
            "calls": llm_calls[k],
            "avg_latency_ms": round(llm_latency[k] / llm_calls[k], 1) if llm_calls[k] else 0,
            "tokens_in": tokens_in[k],
            "tokens_out": tokens_out[k],
        }
        for k in llm_calls
    }
    task_total = sum(task_status.values())
    failed = task_status.get("failed", 0)
    return {
        "uptime_seconds": round(time.time() - started, 1),
        "llm": {
            "total_calls": llm_total,
            "total_cost_usd": round(llm_cost, 6),
            "by_purpose": llm_by_purpose,
        },
        "tasks": {
            "total": task_total,
            "by_status": task_status,
            "failed_ratio": round(failed / task_total, 4) if task_total else 0.0,
            "avg_duration_ms": {
                k: round(task_duration[k] / task_status[k], 1) if task_status[k] else 0
                for k in task_status
            },
        },
        "counters": counters,
    }
