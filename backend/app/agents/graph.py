"""LangGraph 图构建与执行。

流水线（七个阶段）：
  ①产品分析 → ②营销定位 → ③卖点提炼 → ④广告策划 → ⑤合规审查
  → ⑥提示词生成 → ⑦图片生成
"""
from __future__ import annotations

from collections.abc import Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.utils.config import RunnableConfig  # 仅用于类型标注

from app.agents import nodes
from app.agents.state import AgentState

# 各节点对应的进度百分比（用于 SSE 进度展示）
NODE_PROGRESS: dict[str, int] = {
    "analyze_image": 8,       # ① 产品分析
    "market_strategy": 18,    # ② 营销定位
    "sell_points": 30,        # ③ 卖点提炼
    "ad_plan": 42,            # ④ 广告策划
    "compliance_review": 52,  # ⑤ 合规审查
    "prompt_gen": 62,         # ⑥ 提示词生成
    "image_compose": 90,      # ⑦ 图片生成
}


def _after_compliance(state: AgentState) -> str:
    """合规节点后的路由。"""
    compliance = state.get("compliance") or {}
    if compliance.get("passed"):
        return "prompt_gen"
    if state.get("retry_count", 0) >= nodes.MAX_COMPLIANCE_RETRY:
        return "prompt_gen"  # 兜底：重试次数用尽，仍输出（结果中带合规标记）
    return "ad_plan"


def build_graph():
    """构建广告生成 Agent 状态图。"""
    g = StateGraph(AgentState)

    g.add_node("analyze_image", nodes.analyze_image_node)
    g.add_node("market_strategy", nodes.market_strategy_node)
    g.add_node("sell_points", nodes.sell_points_node)
    g.add_node("ad_plan", nodes.ad_plan_node)
    g.add_node("compliance_review", nodes.compliance_review_node)
    g.add_node("prompt_gen", nodes.prompt_gen_node)
    g.add_node("image_compose", nodes.image_compose_node)

    g.add_edge(START, "analyze_image")
    g.add_edge("analyze_image", "market_strategy")
    g.add_edge("market_strategy", "sell_points")
    g.add_edge("sell_points", "ad_plan")
    g.add_edge("ad_plan", "compliance_review")
    g.add_conditional_edges(
        "compliance_review",
        _after_compliance,
        {"ad_plan": "ad_plan", "prompt_gen": "prompt_gen"},
    )
    g.add_edge("prompt_gen", "image_compose")
    g.add_edge("image_compose", END)

    # 挂内存 checkpointer：让 run_graph 执行完能取到图的权威最终状态（get_state 要求
    # 有 checkpointer）。仅在进程内存中，不落盘、不改变执行语义。
    return g.compile(checkpointer=MemorySaver())


_graph = None


def get_graph():
    """返回编译后的图（单例）。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def run_graph(
    initial_state: AgentState,
    on_progress: Callable[[str, int], None] | None = None,
) -> AgentState:
    """执行图，逐节点触发进度回调，返回最终状态。

    每次节点完成会回调 on_progress(node, NODE_PROGRESS[node])；
    同时把 sink 注入 nodes 模块，使长耗时节点在内部推进更精细的百分比。

    返回的是 **LangGraph 权威最终状态**，而非把各节点增量手工 `update` 拼起来：
    `stream(stream_mode="updates")` 每个 chunk 只含该节点的**增量**，两个节点写同一
    个嵌套键时（如 compliance_review 与 refine 都写 copies），后写者会把前者的
    结果整体覆盖掉。原本靠"每个节点自觉返回完整副本"来规避（见
    compliance_review_node 里 `{**state["copies"], "versions": copies}` 的注释），
    是隐性契约——任何新增节点只要返回局部对象就会静默丢字段。
    改为执行后直接取图自己维护的状态，契约由框架保证。
    """
    graph = get_graph()
    config: RunnableConfig = {"configurable": {"thread_id": f"task-{initial_state.get('task_id', 0)}"}}

    def _sink(pct: int, node: str | None = None) -> None:
        # 节点内精细进度：回调 on_progress(node, pct) 以复用同一写库通道
        if on_progress:
            on_progress(node or "", pct)

    # ContextVar 天然按线程隔离（每个任务在独立线程执行），并发任务不会互相覆盖。
    # 用 token 保存/恢复，确保同线程内嵌套或连续执行时正确还原。
    token = nodes.set_progress_sink(_sink)
    try:
        # 仍用 stream 以拿到逐节点进度；其产出内容不再用于拼装结果
        for update in graph.stream(
            initial_state, config=config, stream_mode="updates"
        ):
            for node_name in update:
                progress = NODE_PROGRESS.get(node_name, 0)
                if on_progress:
                    on_progress(node_name, progress)
        snapshot = graph.get_state(config)
        authoritative = dict(snapshot.values or {})
    finally:
        nodes.reset_progress_sink(token)

    # 图状态里没有的键（如 node 内部 emit 的派生信息）以入参兜底，保证调用方拿到的
    # 结果至少包含它传进来的全部字段
    for key, value in initial_state.items():
        authoritative.setdefault(key, value)
    return authoritative  # type: ignore[return-value]
