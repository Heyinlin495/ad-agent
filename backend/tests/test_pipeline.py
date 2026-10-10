"""七阶段生图流水线结构测试（Mock 模式）。"""
from __future__ import annotations

import io
import re

from PIL import Image


def _make_image_bytes() -> bytes:
    img = Image.new("RGB", (400, 400), (200, 180, 160))
    for x in range(0, 400, 4):
        for y in range(0, 400, 4):
            img.putpixel((x, y), (110 + x % 90, 80 + y % 80, 70))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _analyze(client) -> int:
    resp = client.post(
        "/api/v1/products/analyze",
        files={"file": ("p.png", _make_image_bytes(), "image/png")},
    )
    return resp.json()["data"]["product"]["id"]


def _initial_state(product_id: int) -> dict:
    return {
        "task_id": 0,
        "product_id": product_id,
        "country": "US",
        "language": "en",
        "platform": "Amazon",
        "style": "promo",
        "price": "29.99",
        "promotion": "",
        "size_preset": "1:1",
        "num_versions": 3,
        "retry_count": 0,
    }


def test_graph_structure():
    """七个阶段节点齐全，进度值严格递增。"""
    from app.agents.graph import NODE_PROGRESS, build_graph

    graph = build_graph()
    nodes = set(graph.get_graph().nodes)
    expected = {
        "analyze_image", "market_strategy", "sell_points", "ad_plan",
        "compliance_review", "prompt_gen", "image_compose",
    }
    assert expected.issubset(nodes), f"缺少节点：{expected - nodes}"
    assert set(NODE_PROGRESS) == expected
    # 进度必须严格递增（前端进度条不回退）
    ordered = sorted(NODE_PROGRESS.values())
    assert ordered == sorted(set(ordered)), "进度值存在重复"


def test_full_pipeline_and_outputs(client):
    """完整跑通七阶段：卖点提炼、广告主题、提示词、图片全部产出。"""
    from app.agents.graph import run_graph

    product_id = _analyze(client)
    final = run_graph(_initial_state(product_id))

    # ②营销定位：产品类型/目标用户/使用场景
    market = final["market"]
    assert market.get("product_type")
    assert market.get("target_audience")
    assert market.get("usage_scenes")

    # ③卖点提炼：3~5 条核心卖点，每条带依据与优先级
    core = final["core_points"]["core_points"]
    assert 3 <= len(core) <= 5
    assert all(p.get("point") and p.get("evidence") for p in core)

    # ④广告策划：广告主题 + 构图/版式 + 多版本文案
    assert final["copies"]["ad_theme"]
    assert final["copies"]["layout"].get("composition")
    assert len(final["copies"]["versions"]) == 3

    # ⑤合规审查：每个版本都带合规标记
    assert all(v.get("compliance") is not None for v in final["copies"]["versions"])

    # ⑥提示词生成：非穿戴类走 hero 版式 + 背景提示词 + 中央留白约束
    design = final["design"]
    assert design["template_id"] == "hero"
    assert design["image_prompt"]
    assert "center" in design["image_prompt"].lower()

    # ⑦图片生成：产出图片与主体几何
    image = final["images"][0]
    assert image["image_url"].startswith("/files/")
    # 海报模式（默认开启）：主视觉整图重塑 + 多分区排版，不写贴图源尺寸。
    if design.get("_poster"):
        assert image["template_id"] == "poster"
        assert design.get("_compose_mode") == "imagegen"
        assert not design.get("_subject_src"), "生成模式不应写入贴图源尺寸"
        sections = design.get("_poster_sections") or {}
        assert sections, "海报模式应记录分区信息"
        assert sections.get("grid") is True, "有卖点时应渲染卖点网格"
    # 单图链路：整图重塑模式下主体是"生成出来的"（无贴图源尺寸），
    # 且主体框仅在 rembg 估计**可靠**时才写入（不可靠时不写，避免误报"文字压主体"）。
    elif design.get("_compose_mode") == "imagegen":
        assert not design.get("_subject_src"), "生成模式不应写入贴图源尺寸"
        assert len(design.get("_subject_box", [])) in (0, 4)
    else:
        assert len(design.get("_subject_box", [])) == 4
        assert len(design.get("_subject_src", [])) == 2


def test_category_tag_and_icon_plural():
    """品类标签/图标要能命中复数品名（headphones/shoes），并对非音频品类用通用图标。"""
    from app.agents.category_rules import category_style_hint, category_tag, product_icon

    hp = {"category": "Consumer Electronics", "name": "Over-Ear Wired Headphones",
          "selling_points": ["Soft ear cushions"]}
    assert category_tag(hp) == "AUDIO"
    assert product_icon(hp) == "bars"
    assert category_style_hint(hp), "耳机应命中数码电子风格规则"

    assert category_tag({"category": "耳机", "name": "无线蓝牙耳机"}) == "AUDIO"
    assert product_icon({"category": "Household Goods", "name": "Blue Water Bottle",
                         "selling_points": []}) == "check"
    assert category_tag({"category": "Consumer Electronics", "name": "Smart LED Television"}) == "ELECTRONICS"
    # 复数鞋服应命中鞋服规则
    assert category_style_hint({"category": "Apparel", "name": "Running Shoes"})


def test_worn_apparel_uses_magazine_template():
    """穿戴类商品走 magazine 杂志版式（文字落一侧），非穿戴类走 hero。"""
    from app.agents.category_rules import is_worn_apparel
    from app.agents.nodes import _scene_prompt

    jacket = {"name": "Cropped Wool Jacket", "category": "apparel", "color": "beige",
              "material": "wool"}
    assert is_worn_apparel(jacket) is True

    # scene_prompt 兜底：必须描述商品本身（而非只描述背景）
    d = _scene_prompt(jacket, {"text_area": "left"})
    assert d and "beige" in d.lower() and "jacket" in d.lower()
    assert "right side" in d.lower(), "文字在左时商品应偏右"
    assert "large empty clean space" not in d.lower(), "整图重塑不应要求中央留白"

    # 非穿戴类：scene_prompt 兜底也应给出商品描述（由 LLM 决定是否使用）
    phone = {"name": "Wireless Headphones", "category": "Consumer Electronics"}
    assert is_worn_apparel(phone) is False


def test_scene_negative_drops_product_words():
    """scene_prompt 的负面词必须剔除 product/clothing 等，否则商品会被抹掉。"""
    from app.agents.nodes import _scene_negative

    neg = _scene_negative({"negative_prompt": "product, clothing, garment, people, blurry"})
    low = neg.lower()
    assert "product" not in low
    assert "clothing" not in low
    assert "garment" not in low
    assert "people" in low and "blurry" in low
    # 去重：重复的 people/human 只保留一次
    assert low.count("people") == 1


def test_non_apparel_scene_prompt_is_still_life_not_mannequin():
    """回归：非穿戴类走通道 A 兜底时，**不得**套用服装模板。

    线上事故：鼠标任务 LLM 正确地把 scene_prompt 留空（非穿戴类），但兜底提示词
    是服装专用模板（headless mannequin + 挂衣杆 + 手袋），结果生成"鼠标摆在人肩膀上"。
    非穿戴类的兜底必须是静物陈列（hero product on a surface），且场景复用 image_prompt。
    """
    from app.agents.category_rules import is_worn_apparel
    from app.agents.nodes import _scene_prompt

    mouse = {"name": "Logitech Wired USB Optical Mouse",
             "category": "Computer Peripherals", "material": "", "color": ""}
    assert is_worn_apparel(mouse) is False

    design = {
        "scene_prompt": "",  # LLM 对非穿戴类留空 → 走兜底
        "text_area": "bottom",
        "image_prompt": (
            "Premium commercial product photography background for electronics. "
            "Deep charcoal acoustic foam geometric wall with subtle angular texture. "
            "Large empty clean space in the center of the frame for product placement. "
            "No objects, no people, no hands in center area. "
            "Photorealistic material rendering, 8k, ultra detailed."
        ),
    }
    sp = _scene_prompt(mouse, design)
    low = sp.lower()

    # 1) 商品本身必须被描述
    assert "mouse" in low
    # 2) 严禁服装模板措辞（这就是线上 bug 的根因）
    for bad in ("mannequin", "clothing rail", "hanger", "fabric texture",
                "warm minimalist studio", "boucle", "dried branches"):
        assert bad not in low, f"非穿戴类兜底不应出现服装模板词: {bad}"
    # 3) 必须改为静物陈列，且有真实承托面
    assert "hero product" in low
    assert any(k in low for k in ("surface", "desk", "pedestal", "slab"))
    # 4) 贴图用的"中央留白"约束不应残留（整图重塑下商品就在画面里）
    assert "large empty clean space" not in low
    # 5) 应复用 image_prompt 的场景/灯光描述
    assert "acoustic foam" in low


def test_scene_negative_drops_category_entity_words():
    """回归：通道 A 负面词必须剥离品类实体词。

    线上事故：鼠标任务的 negative_prompt 含 `mouse, keyboard, laptop, monitor`，
    这些是通道 B 为"背景别出现杂物"写的；通道 A 整图重塑下照搬会把商品本身抹掉。
    """
    from app.agents.nodes import _scene_negative

    neg = _scene_negative({
        "negative_prompt": "people, blurry, product, mouse, keyboard, laptop, monitor, "
                           "cable, phone, notebook, cup, sofa, plant",
    })
    low = neg.lower()
    for bad in ("mouse", "keyboard", "laptop", "monitor", "cable", "phone",
                "notebook", "cup", "product"):
        assert not re.search(rf"(^|, ){bad}(,|$)", low), f"负面词应剥离: {bad}"
    # 仍应保留有效负面词与背景约束
    assert "people" in low and "blurry" in low and "sofa" in low
    # 复数形式也要能剥离
    neg2 = _scene_negative({"negative_prompt": "mouses, mices, keyboards, laptops"})
    assert "keyboard" not in neg2.lower()


def test_worn_apparel_scene_prompt_keeps_mannequin_template():
    """反向回归：真正的服装兜底仍须走人台模板（本次修复不能误伤穿戴类）。"""
    from app.agents.nodes import _scene_prompt

    jacket = {"name": "Beige Cropped Wool Jacket", "category": "Apparel",
              "material": "wool", "color": "beige"}
    sp = _scene_prompt(jacket, {"scene_prompt": "", "text_area": "left"})
    low = sp.lower()
    assert "mannequin" in low
    assert "clothing rail" in low
    assert "fashion editorial" in low
    assert "hero product" not in low


def test_llm_scene_prompt_passed_through_unchanged():
    """LLM 已给出合格 scene_prompt 时必须原样使用，不得被兜底覆盖。"""
    from app.agents.nodes import _scene_prompt

    custom = ("A premium still life of the product on a polished walnut desk with soft "
              "window light, cool blue rim light, editorial composition with generous "
              "left negative space, sharp details, 8k")
    out = _scene_prompt(
        {"name": "Logitech Mouse", "category": "Computer Peripherals"},
        {"scene_prompt": custom, "text_area": "left"},
    )
    assert out == custom


def test_magazine_layout_keeps_text_off_subject():
    """magazine 排版：文字框与主体框不相交时不应打衬底；相交时才加蒙版。"""
    import io

    from PIL import Image

    from app.image.composer import compose_ad_from_image

    # 造一张左半干净、右半深色的"主体"图，模拟场景大片
    canvas = Image.new("RGB", (1080, 1350), (238, 232, 220))
    for y in range(1350):
        for x in range(560, 1080):
            canvas.putpixel((x, y), (52, 60, 78))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")

    design = {"template_id": "magazine", "text_area": "left",
              "palette": ["#1B1B1F", "#B08A5A"],
              "_brand": "FASHION & STYLE", "_tag": ""}
    compose_ad_from_image(
        buf.getvalue(), design, "Timeless\nElegance", "Classic Looks",
        "SHOP NOW", "4:5", bullets=["Premium Fabric", "Comfortable Fit", "Versatile Style"],
    )
    assert design.get("_compose_mode") == "imagegen"
    assert list(design.get("_text_box")), "应写入文字框（版式调试与回归比对用）"
    # 主体框仅在 rembg 估计可靠时写入；纯色合成图无法可靠估计，允许缺失
    assert len(design.get("_subject_box", [])) in (0, 4)
    # 文字框必须落在画面左侧留白区（magazine 版式默认左文右图）
    tb = design["_text_box"]
    assert tb[0] < 1080 * 0.2 and tb[2] <= 1080 * 0.55


# ---------- 状态合并语义（回归：节点就地修改未回传） ----------

def test_run_graph_returns_authoritative_state(client):
    """run_graph 必须返回图维护的权威状态，而非手工拼装节点增量。

    回归背景：原实现用 `final_state.update(node_output)` 浅合并 stream 增量，
    靠「每个节点自觉返回完整副本」的隐性契约成立。一旦某节点只返回局部对象，
    要合并的嵌套字段就被整体覆盖。改为读 get_state() 的权威快照后，契约由框架保证。
    """
    from app.agents.graph import NODE_PROGRESS, run_graph

    product_id = _analyze(client)
    state = _initial_state(product_id)

    seen_nodes: list[str] = []
    final = run_graph(state, on_progress=lambda n, p: seen_nodes.append(n))

    # 进度回调仍逐节点触发（stream 未被移除）
    assert set(seen_nodes) >= set(NODE_PROGRESS), seen_nodes
    # 入参字段全部保留
    for k in ("product_id", "country", "language", "platform", "size_preset"):
        assert final.get(k) == state.get(k), k
    # 各阶段产出齐全
    assert final["product"] and final["market"] and final["copies"]
    assert final["images"]


def test_composer_geometry_reaches_state_via_design(client, monkeypatch):
    """composer 就地写入的几何信息必须经 design 回传到图状态（贴图链路）。

    回归背景：composer 通过 `design["_subject_box"] = ...` **就地修改**传入的 dict。
    该变更若不由节点显式回传 state，下游读 `state["design"]` 就拿不到
    （`images[0].scheme` 虽引用同一对象，但框架深拷贝后两者分叉），
    版式几何信息被静默丢弃——这正是本轮 run_graph 改造暴露出的缺陷。

    这里强制走降级贴图链路（layout_compose），因为只有该链路会写入 _subject_box。
    """
    from app.agents import nodes
    from app.agents.graph import run_graph

    # 让通道 A（整图重塑）直接失败，逼出 layout_compose 贴图链路
    monkeypatch.setattr(
        nodes, "_scene_prompt", lambda product, design: "", raising=True
    )
    # 通道 A2（图生图）也跳过
    monkeypatch.setattr(nodes, "is_worn_apparel", lambda product: False, raising=True)

    product_id = _analyze(client)
    final = run_graph(_initial_state(product_id))

    design = final["design"]
    scheme = final["images"][0]["scheme"]
    assert design.get("_compose_mode") is None or design.get("_compose_mode") != "imagegen"
    # 核心断言：composer 就地写入的几何信息必须出现在 state["design"] 里
    assert len(design.get("_subject_box", [])) == 4, (
        f"composer 写入的 _subject_box 未回传到 state['design']：{design.get('_subject_box')}"
    )
    assert len(design.get("_subject_src", [])) == 2
    # images[0].scheme 与 design 指向同一份内容
    assert scheme.get("_subject_box") == design.get("_subject_box")
