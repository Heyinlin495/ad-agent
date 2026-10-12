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
from app.image.detail_tiles import (
    DETAIL_ASPECT_RATIO,
    detail_base_variants,
)
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
    # 🚨 海报模式下主视觉文案恒占**左下角一列**（见 poster._HERO_TEXT_COL），
    # 所以必须把 `text_area` 钉成 left：通道 A 的场景提示词按它决定商品位置
    # （文字在左 → 商品偏右、左列留出干净负空间，见 `_scene_prompt`）。
    # 历史缺陷：非穿戴品类默认 `text_area="top"`，`_scene_prompt` 于是把商品放在
    # **左侧**，而海报文字固定在左下角 → 线上实测"主标题被耳机主体遮挡、层次不清"。
    if result.get("poster_mode"):
        result["text_area"] = "left"
    # 海报卖点：标题|一行说明。缺失时由核心卖点兜底（渲染端还会再兜一层）。
    if not result.get("poster_bullets"):
        result["poster_bullets"] = [
            str(p.get("point") or "").strip()
            for p in core_points
            if isinstance(p, dict) and str(p.get("point") or "").strip()
        ][:4]
    # 细节四格标题：LLM 专为"材质/结构/工艺/配件"特写写的 4 条标题，避免与卖点网格重复
    dcaps = result.get("detail_captions") or []
    if isinstance(dcaps, list):
        cleaned = [str(c).strip() for c in dcaps if str(c).strip()]
        if cleaned:
            result["_detail_captions"] = cleaned[:4]
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


def _detail_caption_candidates(product: dict[str, Any], design: dict[str, Any]) -> list[str]:
    """细节四格的 4 条标题候选。

    优先级：`design["_detail_captions"]`（LLM 专为细节图写的"细节向"标题）→ 卖点标题
    → 通用兜底。注意标题必须在**出图之前**定下来，因为它要驱动取景（见 `_detail_prompts`）。
    """
    captions = [str(c).strip() for c in (design.get("_detail_captions") or []) if str(c).strip()]
    if len(captions) >= 4:
        return captions[:4]
    bullets = [str(b).strip() for b in (design.get("_bullets") or []) if str(b).strip()]
    titles = [b.split("|", 1)[0].strip() for b in bullets][:4]
    fallbacks = ["Premium Finish", "Fine Craftsmanship", "Built to Last", "In the Box"]
    while len(titles) < 4:
        titles.append(fallbacks[len(titles)])
    return titles


# 标题不可用时的取景兜底，按"材质 → 结构 → 耐用 → 配件"四个维度
_DETAIL_FOCUS_FALLBACK: tuple[str, ...] = (
    "the surface material and its fine texture",
    "the seams, joints and edge craftsmanship",
    "the load-bearing structure and connection points",
    "the accessories, plugs and connectors",
)


def _focus_phrase(caption: str, fallback: str) -> str:
    """把细节标题整理成提示词里的"取景主体"短语。

    🚨 这是修「四格同机位」的关键一步。旧版提示词只写 `{color} {material} {name}`，
    **标题点名的部件根本没进提示词**——标题写 "PU Leather Cushions"，提示词里连
    cushion 都不出现，模型无从知道该拍哪个局部，只能反复吐"整机 + 灰底"。
    """
    t = re.sub(r"[^\w\s&+.-]", " ", str(caption or ""))
    t = re.sub(r"\s+", " ", t).strip(" .-")
    return t[:56].lower() if len(t) >= 3 else fallback


def _detail_prompts(product: dict[str, Any], captions: list[str]) -> list[tuple[str, str]]:
    """为"细节四格"生成 4 组 (标题, 文生图提示词) —— **标题驱动取景**。

    历史事故一：旧版直接复用卖点标题前 4 条，导致细节带与 2×2 卖点网格**逐字重复**，
    且语义错位（细节图拍的是材质/结构，配文却是使用体验）。

    历史事故二（2026-10-12 实测）：四条"角度"措辞互为同义改写
    （extreme close-up / close-up detail / close-up …），且都不含标题点名的部件 →
    生产图四格两两相关 **+0.78**，实为同一张整机图。现在把标题的部件名直接写进
    取景短语，并加"只呈现该局部、不要整机、不要宽景"的硬约束。
    """
    name = str(product.get("name") or product.get("category") or "the product").strip()
    color = str(product.get("color") or "").strip()
    material = str(product.get("material") or "").strip()
    subject = " ".join(p for p in [color, material, name] if p).strip() or "the product"

    out: list[tuple[str, str]] = []
    for i in range(4):
        cap = captions[i] if i < len(captions) else ""
        focus = _focus_phrase(cap, _DETAIL_FOCUS_FALLBACK[i])
        prompt = (
            f"Professional e-commerce product detail macro photography: extreme close-up "
            f"that fills the entire frame with {focus} of a {subject}; "
            f"only this part is visible and naturally cropped by the frame edges, "
            f"do NOT show the whole product, no full-product wide shot; "
            f"clean neutral light-gray seamless studio backdrop, soft even studio lighting, "
            f"sharp focus, ultra detailed, high resolution, premium commercial catalog style, "
            f"realistic material rendering, no text, no watermark"
        )
        out.append((cap, prompt))
    return out


def _detail_edit_instructions(captions: list[str], retry: bool = False) -> list[str]:
    """"细节四格"的**图生图指令**（以商品图为底）。

    🚨 为什么细节图走图生图：旧版细节图是**独立文生图**（只喂"颜色 + 材质 + 商品名"），
    与主图（通道 A 整图重塑）各自"想象"商品形态 → 实测同一张海报里主图是"没有麦克风
    杆的耳机"、细节图却出现带 boom mic 的同款异形，**一图多款**，电商场景下等于误导。

    🚨 为什么指令必须带标题部件名：2026-10-12 实测四格两两相关 +0.78 —— 四条指令
    只说"推近到表面/接缝/受力/配件"，彼此是同义改写，模型无法区分取景。现在把标题
    部件名作为取景目标写死，并要求"占满整个画面、不要出现完整商品"。

    `retry=True` 用于"雷同闸门"重试：追加一句"换机位"，把撞车的格子推离原构图。
    """
    keep = "保持画面中商品的款式、颜色、材质、结构与配件完全不变，不要增删任何部件；"
    bg = (
        "将该局部置于干净的浅灰色无缝影棚背景中，柔和平滑的影棚打光，"
        "画面干净无杂物、无渐变、无强烈阴影，主体清晰锐利，真实产品摄影质感，"
        "画面中不出现任何文字或水印。"
    )
    out: list[str] = []
    for i in range(4):
        cap = captions[i] if i < len(captions) else ""
        focus = _focus_phrase(cap, _DETAIL_FOCUS_FALLBACK[i])
        out.append(
            f"{keep}把镜头推进到该商品的「{focus}」，让这一局部占满整个画面，"
            f"画面里只保留这个局部、不要再出现完整商品；{bg}"
        )
    if retry:
        out = [
            f"{t}换一个明显不同的机位与构图：相机移到斜侧方重新取景，不要与上一张相同。"
            for t in out
        ]
    return out


# 规格/接口/参数类词：细节标题里出现这些词、但商品信息中**没有**出现过，
# 就属于"编造参数"（虚假宣传风险，且常与主图商品对不上）。
_SPEC_TOKEN_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mm|cm|mah|gb|tb|ghz|mhz|hz|w|v|ml|inch)\b"
    r"|\busb(?:\s?-?\s?c)?\b|\btype\s?-?\s?c\b|\bbluetooth(?:\s?\d(?:\.\d)?)?\b"
    r"|\baux\b|\bjack\b|\bnfc\b|\bhdmi\b|\bipx?\d+\b",
    re.IGNORECASE,
)


def _norm_token(tok: str) -> str:
    return tok.lower().replace(" ", "").replace("-", "")


def _spec_tokens(text: str) -> set[str]:
    """抽取文本里的规格/接口类词（归一化后比较）。"""
    return {_norm_token(m.group(0)) for m in _SPEC_TOKEN_RE.finditer(text or "")}


def _product_text(product: dict[str, Any]) -> str:
    """把商品 dict 里所有值拼成一段文本，用于判断"这个规格是不是已知的"。"""
    parts: list[str] = []
    for v in product.values():
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
        elif isinstance(v, dict):
            parts.extend(str(x) for x in v.values())
        elif v is not None:
            parts.append(str(v))
    return " ".join(parts)


def _resolve_detail_captions(
    product: dict[str, Any],
    titles: list[str],
    grid_titles: set[str],
    n: int,
) -> list[str]:
    """为细节四格定标题：与卖点标题去重，并挡掉"编造参数"的标题。

    🚨 参数幻觉实测事故：商品信息里没有任何接口/规格描述，LLM 却给出了
    "Dual 3.5mm & USB"、"Reinforced Boom Arm"。这类标题属于**虚假宣传**
    （而且细节图与主图本就常对不上），一旦标题里出现"商品信息中不存在的
    规格词"，就改用通用兜底标题。
    """
    known_tokens = _spec_tokens(_product_text(product))
    fallbacks = ["Premium Finish", "Fine Craftsmanship", "Built to Last", "In the Box"]
    caps: list[str] = []
    for i in range(n):
        t = titles[i] if i < len(titles) else ""
        invented = _spec_tokens(t) - known_tokens
        if t and t.strip().lower() not in grid_titles and not invented:
            caps.append(t)
            continue
        if invented:
            logger.warning(
                f"细节标题疑似编造参数，已弃用: {t!r}（商品信息中不存在 {sorted(invented)}）"
            )
        caps.append(fallbacks[i] if i < len(fallbacks) else "Detail View")
    return caps


def _detail_source_bytes(product: dict[str, Any], scene_bytes: bytes) -> bytes:
    """细节图/细节底图的来源图：**优先商品实拍原图**，否则退回主视觉。

    🚨 为什么不用主视觉：海报主视觉是通道 A「整图重塑」的产物，本身是 AI 重画的，
    构图固定，拿它当底/edit 起点只会继承同一构图。商品实拍原图（用户上传）真实、
    清晰、往往多角度，做局部特写远比 AI 主视觉合适。

    读不到原图时安静退回主视觉——细节带是"锦上添花"，不能反过来阻断主流程。
    """
    url = product.get("original_image_url")
    if url:
        try:
            from app.services.storage import storage

            data = storage.absolute_path(url).read_bytes()
            if data:
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"商品原图读取失败，细节图改用主视觉做底: {exc}")
    return scene_bytes


def _ensure_detail_diversity(
    details: list[bytes],
    regenerate: Callable[[int], bytes | None],
    fallback_bytes: bytes | None,
) -> list[bytes]:
    """「四格雷同」闸门（2026-10-12 实测缺陷）。

    生产图四格两两相关均值 **+0.78**、掩膜质心全部落在 (0.48~0.50, 0.61)、主轴角
    164~175° —— 细节带实际是**同一张整机图**。生成侧可能被模型（或欠费降级）悄悄
    压平，而渲染侧此前没有任何把关。

    处置顺序：
    1) 只对**被判雷同**的格子重试一次（指令追加"换机位"）；
    2) 重试后仍雷同 → **整批**改用确定性区域裁切（从上图裁出 4 个互不相同的局部）。
       宁可牺牲一点"AI 质感"，也不让细节带变成四张同一张图。

    任何一步出问题都原样返回 —— 闸门只做增强，不得让细节带变空。
    """
    if len(details) < 2:
        return details
    try:
        from app.image.detail_tiles import (
            DETAIL_ASPECT_RATIO,
            deterministic_detail_crops,
            duplicate_pairs,
            tile_signature,
        )

        sigs = [tile_signature(b) for b in details]
        pairs = duplicate_pairs(sigs)
        if not pairs:
            return details
        logger.warning(f"细节四格疑似雷同，尝试重试: {sorted({i for p in pairs for i in p})}")
        for k in sorted({i for p in pairs for i in p}):
            try:
                nb = regenerate(k)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"细节图 {k} 重试失败: {exc}")
                continue
            if nb:
                details[k] = nb

        sigs = [tile_signature(b) for b in details]
        if duplicate_pairs(sigs) and fallback_bytes:
            crops = deterministic_detail_crops(
                fallback_bytes, len(details), DETAIL_ASPECT_RATIO
            )
            if len(crops) == len(details):
                logger.warning("细节四格重试后仍雷同，改用确定性区域裁切兜底")
                return crops
        return details
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"细节四格雷同检测异常，跳过闸门: {exc}")
        return details


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
    hero_bullets = list(copy0.get("bullets", []) or [])
    # 卖点网格用 标题|说明（取标题段作展示）
    grid_bullets = hero_bullets[:4]
    grid_titles = {str(b).split("|", 1)[0].strip().lower() for b in grid_bullets}

    details: list[bytes] = []
    detail_titles: list[str] = []
    try:
        # 🚨 标题必须在**出图之前**定下来：标题要驱动取景（见 `_detail_prompts`）。
        # 旧版是"先生成、后拼标题"，于是"图拍什么"与"标题写什么"完全脱钩——
        # 标题点名的部件从没进过提示词，这是四格同机位的直接原因。
        captions = _resolve_detail_captions(
            product, _detail_caption_candidates(product, design), grid_titles, 4
        )
        specs = _detail_prompts(product, captions)
        edits = _detail_edit_instructions(captions)
        retry_edits = _detail_edit_instructions(captions, retry=True)

        # 底图：优先商品实拍原图，并按不同的温和取景裁出 4 份**起点不同**的底图。
        # 即使模型很保守，四张编辑的输入本身不同，也不会产出四张一模一样的图。
        src_bytes = _detail_source_bytes(product, scene_bytes)
        bases = detail_base_variants(src_bytes, 4, DETAIL_ASPECT_RATIO)
        if len(bases) < 4:
            bases = [src_bytes] * 4

        # 文生图兜底路径的负面词：显式压制"整机宽景"（与"只拍局部"的目标相反）与
        # "深色/彩色/有杂物"的背景（否则四张会 1 深 3 浅，拼在一起像临时凑的目录图）。
        neg = (
            "people, person, hand, text, letters, watermark, logo, blurry, low quality, "
            "whole product, full product view, wide shot, "
            "dark background, black background, colored background, gradient background, "
            "vignette, busy background, props, clutter"
        )
        tile_size = "detail"          # 3:2，与 poster.DETAIL_TILE_ASPECT 一致

        def _render_tile(i: int) -> bytes | None:
            lo, hi = 89 + int(i * 2), 89 + int((i + 1) * 2)
            # ① 首选图生图：以商品图为底 → 细节图与主图必然是同一款商品，背景也统一
            try:
                return _generate_with_progress(
                    lambda: image_edit(bases[i], edits[i], tile_size), lo, hi
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"细节特写图 {i} 图生图失败，回退文生图: {exc}")
            # ② 兜底：独立文生图（商品一致性无法保证，但保证细节带不为空）
            try:
                return _generate_with_progress(
                    lambda: image_generate(specs[i][1], tile_size, neg), lo, hi
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"细节特写图 {i} 生成失败，跳过: {exc}")
                return None

        slots: list[int] = []
        for i in range(4):
            b = _render_tile(i)
            if b:
                details.append(b)
                detail_titles.append(captions[i])
                slots.append(i)

        # 🚨 「四格雷同」闸门：只对撞车的格子重试（换机位指令），仍雷同则整批改用
        # 确定性区域裁切。见 `_ensure_detail_diversity`。
        details = _ensure_detail_diversity(
            details,
            regenerate=lambda k: _generate_with_progress(
                lambda: image_edit(
                    bases[slots[k] % len(bases)],
                    retry_edits[slots[k] % len(retry_edits)],
                    tile_size,
                ),
                97,
                98,
            ),
            fallback_bytes=src_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"细节特写图规划失败，细节带留空: {exc}")

    emit(97, "image_compose")
    # 细节带标题：已在出图前定稿（与卖点网格去重、并挡掉"编造参数"，见
    # `_resolve_detail_captions`）；某格生成失败时标题随之少一条，保持图文对齐。
    caps = detail_titles
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
        detail_captions=caps or None,
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
