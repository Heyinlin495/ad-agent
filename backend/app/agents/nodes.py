"""LangGraph 节点实现。

节点逻辑集中于此；品类适配规则见 `category_rules.py`，背景安全兜底见 `bg_guard.py`。
"""
from __future__ import annotations

import contextvars
import json
import re
from collections.abc import Callable
from typing import Any

from loguru import logger

from app.agents.bg_guard import (
    ensure_safe_bg_prompt,
    merge_negatives,
)
from app.agents.category_rules import (
    category_style_hint,
    category_tag,
    is_worn_apparel,
    product_icon,
)
from app.agents.state import AgentState
from app.agents.tools import (
    compliance_check,
    compose_from_generated,
    image_edit,
    image_generate,
    layout_compose,
    poster_compose,
    rag_search,
)
from app.core.exceptions import NotFoundError
from app.prompts import render_prompt
from app.services.llm import get_llm

MAX_COMPLIANCE_RETRY = 2

# 运行期进度回调（由 graph.run_graph 注入，节点内用于推进"进行中"的精细百分比）。
# 用 ContextVar 而非模块级全局：任务由线程池并发执行，模块级变量会被后启动的
# 任务覆盖，导致 A 任务的进度写到 B 任务的通道（进度串台）。ContextVar 在线程
# 内天然隔离，各任务的 sink 互不影响。
_progress_sink: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "ad_progress_sink", default=None
)


def set_progress_sink(cb) -> Any:
    """设置当前任务的进度回调，返回可用于恢复的 token。

    token 供 `reset_progress_sink` 还原上下文，保证同线程内连续/嵌套执行
    （如一次请求触发多次 run_graph）不会把 sink 泄漏给后续调用。
    """
    return _progress_sink.set(cb)


def reset_progress_sink(token: Any) -> None:
    """按 token 还原进度回调（与 set_progress_sink 配对使用）。"""
    try:
        _progress_sink.reset(token)
    except (ValueError, LookupError):
        # token 已被消费（跨上下文 reset）时忽略，回到默认值
        _progress_sink.set(None)


def emit(progress: int, node: str | None = None) -> None:
    """节点内上报精细进度（仅当有 sink 时生效）。"""
    cb = _progress_sink.get()
    if cb is not None:
        cb(progress, node)


def _generate_with_progress(
    fn: Callable[[], Any],
    lo: int,
    hi: int,
    node: str = "image_compose",
    tau: float = 25.0,
) -> Any:
    """在阻塞式出图调用期间，把进度从 lo 渐近推到 hi（不越过 hi），返回 fn 的结果。

    为什么需要：只有 dashscope 出图会在轮询时回调进度，openai / sd / flux 都是
    单次阻塞请求、全程无回调。若不接管，出图这一最耗时阶段进度条会一直停在 lo
    不动，看起来像卡死。这里把调用放进子线程、由本线程（持有进度 sink）按渐近
    曲线持续上报；子线程不持有也不需要使用 sink。
    """
    import math
    import time
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn)
        t0 = time.monotonic()
        while True:
            try:
                return fut.result(timeout=1.0)
            except FutureTimeout:
                frac = 1.0 - math.exp(-(time.monotonic() - t0) / tau)
                emit(int(lo + (hi - lo) * frac), node)


def _load_product(product_id: int) -> dict[str, Any]:
    """从数据库加载产品并序列化为字典。"""
    from app.core.database import SessionLocal
    from app.models import Product

    db = SessionLocal()
    try:
        product = db.get(Product, product_id)
        if product is None:
            raise NotFoundError(f"产品 {product_id} 不存在")
        return {
            "id": product.id,
            "name": product.name,
            "category": product.category,
            "material": product.material,
            "color": product.color,
            "shape": product.shape,
            "scenes": product.scenes,
            "selling_points": product.selling_points,
            "target_audience": product.target_audience,
            "brand_suspected": product.brand_suspected,
            "risk_flags": product.risk_flags,
            "subject_image_url": product.subject_image_url,
            "original_image_url": product.original_image_url,
            "extra": product.extra or {},
        }
    finally:
        db.close()


def analyze_image_node(state: AgentState) -> dict[str, Any]:
    """节点 1：产品分析 —— 加载已识别的产品信息（外观/材质/结构/卖点）。

    识别本身在上传时完成（`product_analysis` 多模态提示词）；此处只做载入 +
    叠加用户在产品卡片上的手工修正（product_override），否则下游文案/视觉
    仍会基于识别原始结果生成（"可编辑但不生效"的根因）。
    """
    product = _load_product(state["product_id"])
    override = state.get("product_override") or {}
    for key in ("name", "category", "material", "color", "selling_points"):
        value = override.get(key)
        if value:
            product[key] = value
    return {"product": product}


def _parallel_rag(
    main_query: str,
    compliance_query: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """并发执行两条独立 RAG 检索；失败/并发异常时回退为顺序执行。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _run(q: str, top_k: int) -> list[dict[str, Any]]:
        return rag_search(q, top_k=top_k)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_main = pool.submit(_run, main_query, 6)
            f_comp = pool.submit(_run, compliance_query, 4)
            return f_main.result(timeout=30), f_comp.result(timeout=30)
    except Exception as exc:  # noqa: BLE001  并发不可用时顺序兜底
        logger.warning(f"并行检索失败，回退顺序检索: {exc}")
        return rag_search(main_query, top_k=6), rag_search(compliance_query, top_k=4)


def market_strategy_node(state: AgentState) -> dict[str, Any]:
    """节点 2：营销定位 —— RAG 检索 + LLM 输出产品类型/目标用户/使用场景与策略。"""
    product = state["product"]
    country = state.get("country", "US")
    platform = state.get("platform", "Amazon")
    category = product.get("category")

    emit(10, "market_strategy")
    query = " ".join(
        [product.get("name", ""), category or "", *product.get("selling_points", [])[:2]]
    )
    # 主检索：文化 / 消费习惯 / 文案范例  +  补充检索：合规规则。
    # 两条检索相互独立，用线程池并发执行以缩短总耗时（各自持有 DB 会话，安全）。
    compliance_query = f"{platform} {country} 广告合规规则 禁用词 夸大宣传"
    sources, compliance_sources = _parallel_rag(query, compliance_query)
    emit(13, "market_strategy")
    # 按 (document_id, 内容前缀) 去重合并
    seen: set[tuple[int, str]] = set()
    merged: list[dict[str, Any]] = []
    for s in sources + compliance_sources:
        key = (s.get("document_id", 0), (s.get("content", "") or "")[:80])
        if key not in seen:
            seen.add(key)
            merged.append(s)

    rag_context = json.dumps(merged, ensure_ascii=False, indent=2)
    emit(16, "market_strategy")
    prompt = render_prompt(
        "market_strategy",
        product=json.dumps(product, ensure_ascii=False),
        country=country,
        platform=platform,
        language=state.get("language", "en"),
        style=state.get("style", "promo"),
        rag_context=rag_context,
    )
    llm = get_llm()
    result = llm.chat_json(
        [{"role": "user", "content": prompt}],
        purpose="market_strategy",
    )
    emit(17, "market_strategy")
    # 定位字段兜底：LLM 漏项时用产品识别结果补齐，保证下游（卖点提炼/策划）有输入
    result.setdefault("product_type", product.get("category") or product.get("name") or "")
    result.setdefault("target_audience", product.get("target_audience") or "")
    result.setdefault(
        "usage_scenes",
        [s for s in (product.get("scenes") or []) if str(s).strip()][:5],
    )
    result.setdefault("rag_sources", merged)
    return {"market": result}


def sell_points_node(state: AgentState) -> dict[str, Any]:
    """节点 3：卖点提炼 —— 生成 3~5 个核心卖点（含事实依据与优先级）。

    提炼结果作为后续文案卖点的唯一来源（`ad_plan` 只允许引用这里）。
    """
    product = state["product"]
    market = state["market"]
    emit(22, "sell_points")
    prompt = render_prompt(
        "sell_points",
        product=json.dumps(product, ensure_ascii=False),
        market=json.dumps(market, ensure_ascii=False),
        country=state.get("country", "US"),
        platform=state.get("platform", "Amazon"),
        language=state.get("language", "en"),
    )
    llm = get_llm()
    result = llm.chat_json(
        [{"role": "user", "content": prompt}],
        purpose="sell_points",
    )
    emit(28, "sell_points")
    # 兜底：LLM 未按格式返回时，退化为产品识别出的原始卖点，保证流程不断
    if not result.get("core_points"):
        result["core_points"] = [
            {
                "point": str(p),
                "type": "功能",
                "evidence": "产品识别结果",
                "priority": i + 1,
            }
            for i, p in enumerate((product.get("selling_points") or [])[:5])
        ]
    result.setdefault("product_type", market.get("product_type", ""))
    result.setdefault("audience", market.get("target_audience", ""))
    result.setdefault("scenario", (market.get("usage_scenes") or [""])[0])
    return {"core_points": result}


def ad_plan_node(state: AgentState) -> dict[str, Any]:
    """节点 4：广告策划 —— 确定广告主题、构图、版式，并产出多版本文案。

    文案卖点只能来自上一步提炼的核心卖点；构图/版式（composition/text_area/
    subject_position/visual_focus）交给下一步的提示词生成节点落实为生图提示词。

    若状态中注入了 edited_copy（局部重生成），则直接使用该文案，跳过 LLM。
    """
    if state.get("edited_copy"):
        return {"copies": {"versions": [state["edited_copy"]]}}

    product = state["product"]
    market = state["market"]
    sell_points = state.get("core_points") or {}
    feedback = (state.get("compliance") or {}).get("feedback")

    prompt = render_prompt(
        "ad_plan",
        product=json.dumps(product, ensure_ascii=False),
        market=json.dumps(market, ensure_ascii=False),
        core_points=json.dumps(sell_points.get("core_points", []), ensure_ascii=False),
        country=state.get("country", "US"),
        language=state.get("language", "en"),
        platform=state.get("platform", "Amazon"),
        style=state.get("style", "promo"),
        price=state.get("price") or "未指定",
        promotion=state.get("promotion") or "无",
        num_versions=str(state.get("num_versions", 3)),
    )
    if feedback:
        prompt += f"\n\n上一版合规审查未通过，反馈意见：{feedback}。请针对性修正。\n"

    emit(34, "ad_plan")
    llm = get_llm()
    result = llm.chat_json(
        [{"role": "user", "content": prompt}],
        purpose="ad_plan",
    )
    emit(40, "ad_plan")
    rag_sources = market.get("rag_sources", [])
    for v in result.get("versions", []):
        v.setdefault("rag_sources", rag_sources)
        v.setdefault("compliance", {})
    layout = result.get("layout") or {}
    copies = {
        "versions": result.get("versions", []),
        "ad_theme": result.get("ad_theme", ""),
        "layout": layout,
    }
    return {"copies": copies}


def compliance_review_node(state: AgentState) -> dict[str, Any]:
    """节点 5：合规审查 —— 检查禁用词 / 夸大宣传 / 侵权。"""
    copies = state["copies"]["versions"]
    rules = state["market"].get("compliance_rules", [])
    country = state.get("country", "US")
    platform = state.get("platform", "Amazon")

    # 汇总所有文案文本用于审查
    texts: list[str] = []
    for c in copies:
        texts.append(c.get("headline", ""))
        texts.append(c.get("subheadline", ""))
        texts.extend(c.get("bullets", []))
        texts.append(c.get("cta", ""))
    copy_text = "\n".join(texts)

    emit(44, "compliance_review")
    report = compliance_check(copy_text, country, platform, rules)
    emit(50, "compliance_review")
    retry_count = state.get("retry_count", 0)
    if not report.passed:
        retry_count += 1

    # 将合规结果写回每个版本
    for v in copies:
        v["compliance"] = {"passed": report.passed}

    return {
        "compliance": report.model_dump(),
        "retry_count": retry_count,
        # 保留广告策划写入的 ad_theme / layout（此前直接重建 copies 会丢失）
        "copies": {**state["copies"], "versions": copies},
    }


def prompt_gen_node(state: AgentState) -> dict[str, Any]:
    """节点 6：提示词生成 —— 把广告策划结论落实为可直接生图的提示词与视觉参数。"""
    product = state["product"]
    market = state["market"]
    copies = state["copies"]
    versions = copies.get("versions") or [{}]
    headline = versions[0].get("headline", "")
    layout = copies.get("layout") or {}
    ad_theme = copies.get("ad_theme") or ""
    core_points = (state.get("core_points") or {}).get("core_points") or []

    prompt = render_prompt(
        "prompt_gen",
        product=json.dumps(product, ensure_ascii=False),
        market=json.dumps(market, ensure_ascii=False),
        ad_theme=ad_theme or "（未指定）",
        layout=json.dumps(layout, ensure_ascii=False),
        core_points=json.dumps(
            [p.get("point") for p in core_points if isinstance(p, dict)], ensure_ascii=False
        ),
        headline=headline,
        size_preset=state.get("size_preset", "1:1"),
    )
    # 品类显式提示：按产品品类/名称关键词指向提示词中对应的背景/灯光方案，提高遵循率
    category_hint = category_style_hint(product)
    if category_hint:
        prompt += f"\n\n【品类适配指令（务必遵守）】\n{category_hint}\n"
    # 用户对背景/风格/色调的额外要求（视觉重生成），注入并提高优先级
    visual_hint = state.get("visual_hint")
    if visual_hint:
        prompt += (
            f"\n\n【用户高度优先的附加要求，务必严格遵守】\n"
            f"请据此调整视觉风格/背景/场景/色调/构图：{visual_hint}\n"
            f"同时仍要服务于主标题「{headline}」的广告文案。"
        )
    emit(55, "prompt_gen")
    llm = get_llm()
    result = llm.chat_json(
        [{"role": "user", "content": prompt}],
        purpose="prompt_gen",
    )
    emit(60, "prompt_gen")
    # 版式：穿戴类商品走 magazine（杂志排版，文字落一侧留白），其余走 hero 大特写。
    # LLM 已按提示词给出建议值，这里做一次兜底校正，避免漏填或给出非法模板。
    worn = is_worn_apparel(product)
    tpl = str(result.get("template_id") or "").strip().lower()
    if tpl not in {"magazine", "hero"}:
        tpl = "magazine" if worn else "hero"
    if worn:
        tpl = "magazine"
    result["template_id"] = tpl
    result["composition"] = tpl
    if tpl == "magazine":
        result.setdefault("text_area", layout.get("text_area") or "left")
        if str(result.get("text_area")).lower() not in {"left", "right"}:
            result["text_area"] = "left"
    else:
        result.setdefault("text_area", layout.get("text_area") or "top")
    # 海报模式：默认开启（电商宣传海报是主目标）。LLM 显式给 false 时才走单图链路。
    if result.get("poster_mode") is None:
        result["poster_mode"] = True
    else:
        result["poster_mode"] = bool(result.get("poster_mode"))
    # 海报卖点：标题|一行说明。缺失时由核心卖点兜底（渲染端还会再兜一层）。
    if not result.get("poster_bullets"):
        result["poster_bullets"] = [
            str(p.get("point") or "").strip()
            for p in core_points
            if isinstance(p, dict) and str(p.get("point") or "").strip()
        ][:4]
    return {"design": result}


# ---------- 背景提示词安全兜底 ----------
# 具体规则见 app/agents/bg_guard.py（含 _BLOCKED_BG_TOKENS / SAFE_BG_PROMPT 等）。


def _scene_prompt(product: dict[str, Any], design: dict[str, Any]) -> str:
    """通道 A「整图重塑」提示词：让 AI 直接画出商品已场景化陈列的完整广告画面。

    优先使用 LLM 给出的 `scene_prompt`；缺失时按**品类**拼兜底提示词
    （保证链路不因 LLM 漏填而退化回贴图）。

    兜底必须分品类：LLM 对非穿戴类商品会**故意留空** scene_prompt（见 prompt_gen.txt
    的"其他品类可为空串"），此时若统一套用服装模板（headless mannequin + 挂衣杆 +
    手袋），数码/美妆等商品会被硬塞进人台，生成"鼠标摆在人肩膀上"这类物料。
    非穿戴类改用**静物陈列**（still-life / product on surface）措辞。
    """
    llm_scene = str(design.get("scene_prompt") or "").strip()
    if len(llm_scene) >= 40:
        return llm_scene

    name = str(product.get("name") or "").strip()
    category = str(product.get("category") or "").strip()
    color = str(product.get("color") or "").strip()
    material = str(product.get("material") or "").strip()
    subject = " ".join(p for p in [color, material, name or category] if p).strip()
    if not subject:
        return ""

    # 文字在左 → 商品偏右；文字在右 → 商品偏左
    side = str(design.get("text_area") or "left").lower()
    pos = "right side" if side == "left" else "left side"
    empty = "left" if side == "left" else "right"

    if is_worn_apparel(product):
        return (
            f"Premium fashion editorial photography: a {subject} elegantly presented on a "
            f"headless mannequin in a warm minimalist studio interior, cream plaster wall, "
            f"slim wooden clothing rail with wooden hangers, a small boucle side table with a "
            f"leather handbag and stacked art books, delicate dried branches in a ceramic vase, "
            f"soft diffused daylight creating gentle wall shadows, the subject positioned on the "
            f"{pos} of the frame, generous clean negative space on the {empty} for text, "
            f"photorealistic fabric texture, accurate color and silhouette, shallow depth of field, "
            f"high-end brand campaign, editorial magazine aesthetic, warm neutral color grading, "
            f"sharp details, high dynamic range, 8k, ultra detailed"
        )

    # 非穿戴类：静物陈列（禁止人台/挂衣杆/穿戴类措辞），场景复用 LLM 的 image_prompt
    scene = _still_life_scene(design)
    return (
        f"Premium commercial product photography: a {subject} displayed as the hero product "
        f"on a clean surface, {scene}"
        f"the product positioned on the {pos} of the frame, generous clean negative space on "
        f"the {empty} for text, accurate shape, color and material rendition, realistic "
        f"reflections and contact shadow, shallow depth of field, high-end e-commerce "
        f"campaign aesthetic"
    )


def _still_life_scene(design: dict[str, Any]) -> str:
    """从 LLM 的 image_prompt 提取静物场景描述，拼进通道 A 兜底提示词。

    image_prompt 是 LLM 为非穿戴类精心写好的背景/场景/灯光描述（只描述环境、
    不含商品），去掉其中的"留白"与"无物体"约束（那是给贴图用的，整图重塑下
    商品本身就在画面里，留着会让模型不敢画商品）后即可直接复用。
    """
    raw = str(design.get("image_prompt") or "").strip()
    if not raw:
        return "in a minimalist studio setting with soft directional lighting, clean seamless backdrop, "

    # 按句子切分，丢弃"留白/无物体/无人"这类约束句，避免与"商品已在画面中"冲突
    drop = re.compile(
        r"(empty clean space|empty space|negative space|for product placement|"
        r"no objects|no people|no hands|no furniture|center of the frame|"
        r"product placement|reserved for|clean space in the)",
        re.IGNORECASE,
    )
    kept = [s.strip() for s in re.split(r"(?<=\.)\s+", raw) if s.strip() and not drop.search(s)]
    if not kept:
        kept = [s.strip() for s in re.split(r"(?<=\.)\s+", raw) if s.strip()][:2]
    scene = " ".join(kept).strip()
    if not scene:
        return "in a minimalist studio setting with soft directional lighting, clean seamless backdrop, "
    # 去掉句末多余句号后统一用一个 ", " 接续后面的商品/构图描述
    scene = scene.rstrip(".")
    # 去重：image_prompt 常以"包装句"开头（如 "Premium commercial product photography
    # background for ..."），与本函数外层前缀重复，直接去掉首句避免提示词冗余
    first_dot = scene.find(".")
    if first_dot != -1 and first_dot < 120:
        head = scene[:first_dot].lower()
        if "product photography" in head or "studio setting" in head:
            scene = scene[first_dot + 1 :].strip()
    return (scene.rstrip(".") or "in a minimal studio setting with soft lighting") + ", "


def _scene_negative(design: dict[str, Any]) -> str:
    """通道 A 的负面词：**不得**包含商品自身或其品类词（否则商品会被抹掉）。

    LLM 写的 negative_prompt 有时会带具体品类词（实测鼠标任务里出现
    `mouse, keyboard, laptop, monitor`），这些是通道 B 为"背景留白"写的
    "别在背景里画科技杂物"约束；但通道 A 是整图重塑，商品本身就在画面里，
    照搬会直接把商品从画面里抹掉。

    剥离策略（两层，避免误伤）：
    1. 通称词（product/clothing/item…）**必定**剥离；
    2. 品类实体词分两种处理：
       - 商品自身命中的词（鼠标任务里的 `mouse`）→ **必定剥离**，否则等于让模型别画主角；
       - 商品未命中的"跨品类杂物"词（`laptop`/`monitor`/`cable`）→ 也剥离，
         它们是通道 B 为背景留白写的"别出现杂物"约束，通道 A 下会误伤。
    注意 `bag`/`shoe`/`watch` 这类词：若商品本身就是它，剥离是**正确**的（不能压制主角）；
    若商品不是它，剥离同样正确（背景别出现无关实物）。两种情况都剥离。
    """
    base = str(design.get("negative_prompt") or "")
    always_drop = (
        "product", "clothing", "garment", "apparel", "item", "goods",
        "jacket", "dress", "shirt", "tshirt",
    )
    # 通道 A 下必须全部剥离：要么是商品自己（不能压制主角），要么是背景杂物（不该出现）
    _cross_category = (
        "mouse", "keyboard", "laptop", "notebook", "computer", "monitor",
        "tablet", "phone", "smartphone", "headphone", "earbud", "camera",
        "cable", "wire", "charger", "cup", "mug", "bottle", "bag", "wallet",
        "shoe", "sneaker", "watch", "glasses", "sunglasses", "hat", "scarf",
        "pen", "paper clip", "stapler",
    )
    drop = list(always_drop) + list(_cross_category)

    for bad in drop:
        base = re.sub(rf"\b{re.escape(bad)}(?:s|es)?\b,?\s*", "", base, flags=re.IGNORECASE)
    extra = (
        "people, person, human, face, hand, extra objects, duplicate, distorted, "
        "deformed, melted, low quality, blurry, noise, oversaturated, cartoon, "
        "illustration, anime, fake text, random letters, watermark, bad anatomy"
    )
    # 去重（保留顺序），避免负面词重复堆叠
    seen: set[str] = set()
    merged: list[str] = []
    for tok in f"{base}, {extra}".split(","):
        t = tok.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            merged.append(t)
    return ", ".join(merged)


def _imagegen_instruction(product: dict[str, Any], design: dict[str, Any]) -> str:
    """图生图编辑指令：产品保真是最高优先级——只允许改背景/去无关杂物，不得改产品本身。

    文案（标题/卖点/CTA）由合成器的文字层渲染，指令中绝不要求模型生成文字。
    """
    name = str(product.get("name") or "服装").strip()[:40]
    scene = str(design.get("scene") or "").strip()
    scene_part = f"将背景替换为：{scene}。" if scene else "将背景替换为高级摄影棚纯净场景。"
    return (
        f"保持图片中模特所穿戴的{name}的款式、颜色、图案、材质与细节完全不变，"
        f"保持模特五官样貌与姿势自然，不得更换、新增或删减服装；"
        f"若画面中有手持手机、镜子边框等与商品无关的杂物，将其自然移除并补全被遮挡的手部与身体区域；"
        f"{scene_part}"
        f"专业电商时尚广告摄影，柔和影棚灯光，画面干净简洁有质感，"
        f"人物与服装完整呈现不裁切，主体清晰锐利，真实照片质感，高清细节。"
    )


def _detail_prompts(product: dict[str, Any], design: dict[str, Any]) -> list[tuple[str, str]]:
    """为"细节四格"生成 4 组 (标题, 出图提示词)。

    标题取自卖点（`标题|说明` 的标题段），最多 4 个；不足 4 个时用通用细节角度补齐
    （外观/材质/做工/配件），保证细节带不空。出图提示词描述该卖点对应的**产品特写**。
    """
    name = str(product.get("name") or product.get("category") or "the product").strip()
    color = str(product.get("color") or "").strip()
    material = str(product.get("material") or "").strip()
    subject = " ".join(p for p in [color, material, name] if p).strip()

    bullets = [str(b).strip() for b in (design.get("_bullets") or []) if str(b).strip()]
    titles = [b.split("|", 1)[0].strip() for b in bullets][:4]
    fallbacks = ["Premium Finish", "Fine Craftsmanship", "Built to Last", "In the Box"]
    while len(titles) < 4:
        titles.append(fallbacks[len(titles)])

    angles = [
        "extreme close-up macro shot focusing on the surface material and texture",
        "close-up detail shot of the fine construction and edges",
        "close-up shot highlighting the durable build quality",
        "close-up of the product accessories and connectors",
    ]
    out: list[tuple[str, str]] = []
    for i in range(4):
        p = (
            f"Professional e-commerce product detail photography: extreme close-up of a "
            f"{subject}, {angles[i]}, clean neutral light-gray seamless studio backdrop, "
            f"soft even studio lighting, sharp focus, ultra detailed, high resolution, "
            f"premium commercial catalog style, realistic material rendering, no text, no watermark"
        )
        out.append((titles[i], p))
    return out


def _render_poster(
    scene_bytes: bytes,
    design: dict[str, Any],
    copy0: dict[str, Any],
    product: dict[str, Any],
    size_preset: str,
    emit: Callable[[int, str], None],
) -> dict[str, Any]:
    """海报模式：生成细节特写图 + 合成多分区海报，返回节点结果 dict。

    细节图任何一张失败都跳过（细节带可少几格），不影响主视觉。
    """
    details: list[bytes] = []
    detail_titles: list[str] = []
    try:
        specs = _detail_prompts(product, design)
        neg = "people, person, hand, text, letters, watermark, logo, blurry, low quality"
        # 进度区间 89→97，四张均分
        for i, (title, prompt) in enumerate(specs):
            try:
                b = _generate_with_progress(
                    lambda p=prompt: image_generate(p, "1:1", neg),
                    89 + int(i * 2),
                    89 + int((i + 1) * 2),
                )
                details.append(b)
                detail_titles.append(title)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"细节特写图 {i} 生成失败，跳过: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"细节特写图规划失败，细节带留空: {exc}")

    emit(97, "image_compose")
    hero_bullets = copy0.get("bullets", []) or []
    # 卖点网格用 标题|说明；细节标题复用卖点标题，保证两处措辞一致
    grid_bullets = list(hero_bullets)[:4]
    scenes = [str(s).strip() for s in (product.get("scenes") or []) if str(s).strip()][:4]

    image_url = poster_compose(
        scene_bytes,
        design,
        copy0.get("headline", ""),
        copy0.get("subheadline", ""),
        copy0.get("cta", ""),
        size_preset,
        bullets=grid_bullets,
        detail_image_bytes=details or None,
        scenes=scenes or None,
        tagline=(
            f"{(design.get('_brand') or '').strip()}, "
            f"{(copy0.get('headline') or '').strip()[:32]}"
        ).strip(", "),
    )
    # 标记合成模式：海报主视觉来自整图重塑（无贴图源），供下游/测试判定
    design["_compose_mode"] = "imagegen"
    design["_poster_sections"] = {
        "grid": bool(grid_bullets),
        "details": len(details),
        "footer": bool(scenes),
    }
    return {
        "images": [{
            "image_url": image_url,
            "template_id": "poster",
            "size": size_preset,
            "scheme": design,
        }],
        "design": design,
    }


def image_compose_node(state: AgentState) -> dict[str, Any]:
    """节点 7：图片生成 —— 生成背景主视觉并与真实产品合成（保证产品不变形）。

    注意：各 composer 会**就地修改**传入的 design（写入 `_compose_mode` /
    `_subject_box` / `_subject_src` 等几何信息）。因此本节点必须把这些
    变更**显式回传**到 state（`"design": design`），否则下游读 `state["design"]`
    时看不到它们——`images[0].scheme` 只是同一个对象的引用，框架深拷贝后两者会分叉。
    """
    design = dict(state["design"])  # 浅拷贝：避免直接改动框架持有的 state 对象
    product = state["product"]
    copies = state["copies"]["versions"]
    copy0 = copies[0]
    size_preset = state.get("size_preset", "1:1")
    subject_url = product.get("subject_image_url") or product.get("original_image_url")

    # hero 版式注入按产品自适应的元素（合成器读取，下划线键不进入 LLM 提示词）：
    # 右上角品类标签 / 品牌行 / 底部场景图标行 / 图标形状
    design["_tag"] = category_tag(product)
    design["_brand"] = (product.get("brand_suspected") or "").strip()
    design["_scenes"] = [
        str(s).strip() for s in (product.get("scenes") or []) if str(s).strip()
    ][:3]
    design["_icon"] = product_icon(product)
    # 海报模式的卖点（标题|说明）在此缓存，供细节图规划读取（避免在 _render_poster
    # 里再回头解 copy）
    design["_bullets"] = list(copy0.get("bullets", []) or [])
    # 海报模式开关：由策划/提示词节点写入 design["_poster"]；未开启则走单图链路
    if "_poster" not in design:
        design["_poster"] = bool(design.get("poster_mode"))

    # 通道 A（最高优先）：整图重塑 —— 让 AI 直接生成"商品已场景化陈列"的完整广告画面。
    # 这是对齐商业大模型效果的关键：商品与场景/光影/材质浑然一体，而不是"空背景 + 贴产品"。
    # 失败时依次降级：指令编辑 → 抠图贴图。
    scene_prompt = _scene_prompt(product, design)
    if scene_prompt:
        try:
            emit(62, "image_compose")
            scene_neg = _scene_negative(design)
            scene_bytes = _generate_with_progress(
                lambda: image_generate(scene_prompt, size_preset, scene_neg),
                62,
                89,
            )
            emit(89, "image_compose")
            # 海报模式（多分区）：主视觉 + 卖点网格 + 细节四格 + 底部场景条。
            # 由 design["_poster"] 开关控制（提示词/策划节点可开启）。任何一步失败
            # 都退回"单图叠加"链路，保证不会因海报素材缺失而整体失败。
            if design.get("_poster"):
                try:
                    return _render_poster(
                        scene_bytes, design, copy0, product, size_preset, emit)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"海报模式合成失败，退回单图叠加: {exc}")
            image_url = compose_from_generated(
                scene_bytes,
                design,
                copy0.get("headline", ""),
                copy0.get("subheadline", ""),
                copy0.get("cta", ""),
                size_preset,
                bullets=copy0.get("bullets", []),
            )
            return {
                "images": [{
                    "image_url": image_url,
                    "template_id": design.get("template_id", "magazine"),
                    "size": size_preset,
                    "scheme": design,
                }],
                # 回传 composer 就地修改后的 design，保证图状态为最终版本
                "design": design,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"整图重塑出图失败，降级为指令编辑: {exc}")


    # 通道 A2（兜底）：指令编辑 —— 基于原图做图生图（穿戴类，原图可用时）
    # 模特实拍时人与服装/手机遮挡粘连，抠图难以干净分离，改为基于原图做指令编辑，
    # 产品保真由"只改背景/去杂物、不改产品"的指令约束保证。
    if is_worn_apparel(product) and product.get("original_image_url"):
        try:
            emit(60, "image_compose")
            from app.services.storage import storage

            base_bytes = storage.absolute_path(product["original_image_url"]).read_bytes()
            edited = _generate_with_progress(
                lambda: image_edit(
                    base_bytes,
                    _imagegen_instruction(product, design),
                    size_preset,
                ),
                60,
                89,
            )
            emit(89, "image_compose")
            image_url = compose_from_generated(
                edited,
                design,
                copy0.get("headline", ""),
                copy0.get("subheadline", ""),
                copy0.get("cta", ""),
                size_preset,
                bullets=copy0.get("bullets", []),
            )
            return {
                "images": [{
                    "image_url": image_url,
                    "template_id": design.get("template_id", "hero"),
                    "size": size_preset,
                    "scheme": design,
                }],
                # 回传 composer 就地修改后的 design，保证图状态为最终版本
                "design": design,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"图生图生成失败，降级为抠图合成: {exc}")

    background: bytes | None = None
    image_prompt = ensure_safe_bg_prompt(design.get("image_prompt", ""))
    negative_prompt = merge_negatives(design.get("negative_prompt") or "")
    if image_prompt:
        try:
            emit(64, "image_compose")  # 进入图像生成（耗时主要集中在此）
            background = _generate_with_progress(
                lambda: image_generate(image_prompt, size_preset, negative_prompt),
                64,
                88,
            )
            emit(88, "image_compose")
        except Exception as exc:  # noqa: BLE001  图像生成失败则纯色兜底
            logger.warning(f"图像生成失败，使用纯色背景: {exc}")

    emit(89, "image_compose")
    image_url = layout_compose(
        subject_url,
        background,
        design,
        copy0.get("headline", ""),
        copy0.get("subheadline", ""),
        copy0.get("cta", ""),
        size_preset,
        bullets=copy0.get("bullets", []),
    )
    result = {
        "image_url": image_url,
        "template_id": design.get("template_id", "centered"),
        "size": size_preset,
        "scheme": design,
    }
    # 回传 composer 就地修改后的 design（_subject_box / _subject_src 等几何信息）
    return {"images": [result], "design": design}
