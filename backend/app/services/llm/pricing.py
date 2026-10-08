"""模型价格表与成本估算。

默认单价为**示例值（USD / 每百万 token）**，仅用于给出量级参考，请以实际账单校准，
并可通过配置项 `MODEL_PRICING` 覆盖，例如：
    MODEL_PRICING={"qwen-flash": [0.05, 0.4], "qwen-vl-max": [0.8, 3.2]}
键为模型名（小写、支持前缀匹配），值为 [输入单价, 输出单价]。
"""
from __future__ import annotations

from app.core.config import settings

# 默认单价：{模型名: (输入, 输出)} —— 单位 USD / 1M tokens（示例值，非官方报价）
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # 通义千问（文本）
    "qwen-flash": (0.05, 0.40),
    "qwen-turbo": (0.05, 0.20),
    "qwen-plus": (0.40, 1.20),
    "qwen-max": (1.60, 6.40),
    # 通义千问（视觉）
    "qwen-vl-plus": (0.22, 0.66),
    "qwen-vl-max": (0.80, 3.20),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    # Moonshot / Kimi（原生多模态；推理 token 计入输出）
    "kimi-k3": (3.00, 15.00),
    "kimi-k2.6": (0.95, 4.00),
    "kimi-k2.7-code": (0.95, 4.00),
    "kimi-k2.7-code-highspeed": (1.90, 8.00),
    # Anthropic
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
}


def pricing_table() -> dict[str, tuple[float, float]]:
    """合并默认价格表与配置覆盖项（配置优先）。"""
    table = dict(DEFAULT_PRICING)
    for name, price in (settings.model_pricing or {}).items():
        if isinstance(price, (list, tuple)) and len(price) >= 2:
            try:
                table[str(name).lower()] = (float(price[0]), float(price[1]))
            except (TypeError, ValueError):
                continue
    return table


def _lookup(model: str, table: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    m = model.lower()
    if m in table:
        return table[m]
    # 前缀 / 子串匹配，兼容带日期后缀或厂商前缀的模型名
    best: tuple[str, tuple[float, float]] | None = None
    for key, val in table.items():
        if m.startswith(key) or key in m:
            if best is None or len(key) > len(best[0]):
                best = (key, val)
    return best[1] if best else None


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """按每百万 token 单价估算一次调用成本（USD）。未命中模型返回 0.0。"""
    if not model:
        return 0.0
    price = _lookup(model, pricing_table())
    if price is None:
        return 0.0
    cost = (tokens_in or 0) / 1_000_000 * price[0] + (tokens_out or 0) / 1_000_000 * price[1]
    return round(cost, 6)
