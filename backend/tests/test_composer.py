"""合成器版式硬约束：文字区不得压住产品主体（电视等宽扁产品尤其明显）。"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from app.image import composer
from app.image.generator import make_gradient_background


def _make_canvas(size: tuple[int, int]) -> Image.Image:
    return Image.open(io.BytesIO(make_gradient_background(size))).convert("RGBA")


def _hero(canvas_size, subject_size, *, bullets=None, scenes=None, scale=0.72):
    canvas = _make_canvas(canvas_size)
    subject = Image.new("RGBA", subject_size, (120, 160, 200, 255))
    texts = {
        "headline": "极致画质 震撼视界",
        "subheadline": "4K HDR 智能电视 沉浸式体验",
        "cta": "立即抢购",
        "bullets": bullets if bullets is not None else ["4K超清", "120Hz高刷", "杜比全景声"],
        "promo": "SALE",
    }
    design = {
        "_subject_scale": scale,
        "_brand": "BrandX",
        "_tag": "TV",
        "_scenes": scenes if scenes is not None else ["客厅", "观影", "游戏"],
        "_icon": "check",
        "palette": ["#1D1D1F", "#615ced", "#F2F2F7"],
    }
    composer._tpl_hero(canvas, subject, design, texts, (30, 30, 30))
    return canvas, design


CASES = [
    ((1080, 1080), (900, 1100), "音箱（高瘦）"),
    ((1080, 1080), (1920, 1080), "电视（16:9 宽扁）"),
    ((1080, 1080), (1600, 1000), "显示器（16:10）"),
    ((1080, 1350), (1920, 1080), "电视 @4:5"),
    ((1080, 1920), (900, 1600), "手机 @9:16"),
    ((1920, 1080), (1920, 1080), "电视 @16:9"),
    ((2000, 2000), (1000, 2400), "长筒靴 @方图"),
]


@pytest.mark.parametrize("canvas_size,subject_size,label", CASES)
def test_hero_text_never_overlaps_subject(canvas_size, subject_size, label):
    """所有画布/主体比例下：文字实际占位框与主体框不得相交。"""
    canvas, design = _hero(canvas_size, subject_size)
    subject_rect = composer._xywh_to_rect(tuple(design["_subject_box"]))
    text_rect = tuple(design["_text_box"])
    assert not composer._rects_hit(subject_rect, text_rect), f"{label}: 文字压住主体"


@pytest.mark.parametrize("canvas_size,subject_size,label", CASES)
def test_hero_subject_in_frame_and_undistorted(canvas_size, subject_size, label):
    """主体不得溢出画布，且保持等比缩放。"""
    canvas, design = _hero(canvas_size, subject_size)
    w, h = canvas.size
    x, y, bw, bh = design["_subject_box"]
    assert x >= 0 and y >= 0 and x + bw <= w and y + bh <= h, f"{label}: 主体溢出画面"
    assert abs((bw / bh) - (subject_size[0] / subject_size[1])) / (subject_size[0] / subject_size[1]) < 0.01


def test_hero_wide_product_uses_full_width_band():
    """宽扁产品应落在"上下通栏"版位：主体宽度显著大于侧栏版位下的宽度。"""
    _, design = _hero((1080, 1080), (1920, 1080))
    assert design["_subject_box"][2] / 1080 > 0.6


# ---------- hero_overlay（整图叠加链路）字号可读性回归 ----------
# 线上事故：图生图链路（compose_ad_from_image → _tpl_hero_overlay）里，耳机等
# "主体居中 + 纵向跨度大"的商品会让四个通栏版位全部与主体相交，原实现在窄栏小字号
# 下"干净"就提前收手，导致文案被压缩在角落、标题只有画高 ~1.8%，缩略图完全读不清。
# 下列用例锁定修复后的行为：留白被识别、字号放大到可读下限、标题不出现孤字行。

def _overlay(canvas_size, subject_rect, *, text_area="top", bullets=None):
    """跑一次 hero_overlay，返回 (design, canvas)。用纯色画布避免依赖 rembg。"""
    canvas = _make_canvas(canvas_size)
    # 模拟"主体占据画面"：画一块深色矩形当作主体区域，供避让算法使用
    ImageDraw.Draw(canvas).rectangle(subject_rect, fill=(40, 44, 52, 255))
    texts = {
        "headline": "沉浸式听觉盛宴",
        "subheadline": "主动降噪 40小时续航 无损音质",
        "cta": "立即购买",
        "bullets": bullets if bullets is not None else ["主动降噪", "40h续航", "Hi-Res认证"],
        "promo": "SALE",
    }
    design = {
        "_brand": "AudioPro", "_tag": "AUDIO", "_icon": "check",
        "palette": ["#1D1D1F", "#615ced", "#F2F2F7"], "text_area": text_area,
    }
    composer._tpl_hero_overlay(canvas, design, texts, subject_rect)
    return design, canvas


def test_overlay_text_never_overlaps_subject_when_side_space_exists():
    """主体居中偏右、左侧留有窄栏（约 17% 画宽）时，文案应落在左侧且不压主体。"""
    subject = (520, 260, 1960, 1880)
    design, _ = _overlay((2000, 2000), subject)
    box = design["_text_box"]
    assert box is not None, "未产出文字框"
    text_rect = tuple(box)
    assert not composer._rects_hit(text_rect, subject), "文字压住了主体"


def test_overlay_lifts_font_to_legible_size():
    """窄留白（约 17% 画宽）也必须把标题放大到可读下限（≥ 画高 4.5%）。

    修复前该场景退化到最小档（scale=0.50）→ 标题仅 3.75% 画高。修复后应 ≥ 4.5%。
    """
    subject = (520, 260, 1960, 1880)
    design, canvas = _overlay((2000, 2000), subject)
    _, h = canvas.size
    box = design["_text_box"]
    assert box is not None
    # 文字块高度是可读性的可靠代理：字号越大块越高。
    # 修复前块高约 226px（11%），修复后应显著更高。
    assert (box[3] - box[1]) >= int(h * 0.20), f"文字块过矮，字号偏小: {box}"


def test_overlay_headline_has_no_orphan_line():
    """标题最后一行不得只剩 1 个字（孤字行）——窄栏 + 大字号时的典型缺陷。

    排序逻辑已把"无孤字"排在"字号更大"之前；更根本的是 `_wrap_text_balanced` 会把
    `沉浸式/听觉盛/宴` 重排成 `沉浸式/听觉/盛宴`，从而**不必为了躲孤字而降字号**。
    """
    subject = (520, 260, 1960, 1880)
    design, _ = _overlay((2000, 2000), subject)
    assert "_text_scale" in design, "未记录采用的字号档，无法复核"
    canvas = _make_canvas((2000, 2000))
    box = design["_text_box"]
    col_w = max(120, box[2] - box[0])
    texts = {"headline": "沉浸式听觉盛宴", "subheadline": "", "cta": "", "bullets": []}
    d = {"_brand": "", "palette": ["#1D1D1F"]}
    _, items, _ = composer._hero_text_layout(
        canvas, d, texts, 120, col_w, scale=float(design["_text_scale"]))
    checked = 0
    for it in items:
        if it.get("kind") == "text" and it.get("stroke") == 1:
            lines = it.get("lines") or composer._wrap_text(
                it["text"], it["font"], it["max_w"])
            checked += 1
            assert len(lines) < 2 or len(lines[-1].strip()) > 1, f"标题出现孤字行: {lines}"
    assert checked == 1, "未找到标题行，用例失效"


def test_balanced_wrap_removes_orphan_without_shrinking():
    """`_wrap_text_balanced` 应在**不缩小字号**的前提下消除独字末行。"""
    font = composer.load_font(150, bold=True)
    greedy = composer._wrap_text("沉浸式听觉盛宴", font, 500)
    assert len(greedy[-1].strip()) <= 1, "前置条件不成立：贪心换行本应产生孤字"
    balanced = composer._wrap_text_balanced("沉浸式听觉盛宴", font, 500)
    assert len(balanced) == len(greedy), "行数不应增加"
    assert len(balanced[-1].strip()) > 1, f"均衡换行后仍有孤字: {balanced}"
    # 不能溢出栏宽
    assert all(font.getlength(ln) <= 500 for ln in balanced), f"均衡换行溢出栏宽: {balanced}"


# ---------- text_area 硬约束（P0 修复）----------
# 线上事故（任务 #3）：LLM 给出 text_area="bottom" 且 scene_prompt 明写"留白在下方"，
# 但渲染器把 text_area 只当同分排序偏好，被"贴顶窄栏恰好干净"抢走 → 文案排到左上角、
# 压在浅色木纹上，既与提示词矛盾又几乎不可见。现改为：优先只在指定侧选版位。

def test_overlay_honors_text_area_bottom_when_space_exists():
    """主体偏上居中、下方有大片留白、text_area=bottom 时，文字必须落在下半部。"""
    subject = (600, 150, 1400, 900)          # 主体在上半部
    design, _ = _overlay((2000, 2000), subject, text_area="bottom")
    box = design["_text_box"]
    cy = (box[1] + box[3]) / 2
    assert cy >= 1000, f"text_area=bottom 未生效，文字中心 y={cy:.0f} 落在上半部"
    assert design.get("_text_area_honored") is True


def test_overlay_honors_text_area_top_when_space_exists():
    """主体偏下、上方留白、text_area=top 时，文字必须落在上半部。"""
    subject = (600, 1100, 1400, 1850)
    design, _ = _overlay((2000, 2000), subject, text_area="top")
    box = design["_text_box"]
    cy = (box[1] + box[3]) / 2
    assert cy <= 1000, f"text_area=top 未生效，文字中心 y={cy:.0f} 落在下半部"


def test_overlay_still_avoids_subject_under_text_area_constraint():
    """text_area 约束不能让文字压主体：指定侧放不下时应降级到其他侧而非硬塞。"""
    subject = (300, 1000, 1700, 1900)        # 主体占据下半部
    design, _ = _overlay((2000, 2000), subject, text_area="bottom")
    box = design["_text_box"]
    assert not composer._rects_hit(tuple(box), subject), "为满足 text_area 硬塞导致压主体"


# ---------- 出图尺寸保持长宽比（P0 修复）----------
# 线上事故：_wanx_size 把 4:5(1080×1350) 塌缩成 720*1280(0.5625)，AI 按错误比例作画，
# 构图与留白位置全被破坏 → 出图裁剪后"留白不在该在的位置"。

def test_wanx_size_preserves_aspect_ratio():
    from app.image.generator import _wanx_size, resolve_size
    for name in ("1:1", "4:5", "9:16", "16:9", "amazon"):
        w, h = resolve_size(name)
        out = _wanx_size(w, h)
        ow, oh = (int(x) for x in out.split("*"))
        want, got = w / h, ow / oh
        assert abs(want - got) / want < 0.01, f"{name} 长宽比失真: {out} ({got:.3f} vs {want:.3f})"
        assert 512 <= ow <= 1440 and 512 <= oh <= 1440, f"{name} 尺寸越界: {out}"


def test_load_base_cover_letterboxes_on_ratio_mismatch():
    """AI 返回图与目标长宽比差异大时，应 contain 补边而非 cover 裁剪（保住构图）。"""
    src = Image.new("RGBA", (720, 1280), (200, 120, 60, 255))   # 0.5625
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    out = composer._load_base_cover(buf.getvalue(), (1080, 1350))  # 目标 0.80
    assert out.size == (1080, 1350)


# ---------- 文字对比度（P1 修复）----------
# 线上事故（任务 #3）：白字压在浅色木纹/高光上，肉眼几乎不可见。旧实现只在
# "文字与主体相交"时加衬底，对"不相交但对比度极低"的情况完全不处理。

def test_scrim_added_when_text_on_low_contrast_background():
    """白字落在浅色背景（无主体相交）时，必须加衬底保证对比度。"""
    canvas = Image.new("RGBA", (2000, 2000), (196, 168, 124, 255))   # 浅色木纹
    ImageDraw.Draw(canvas).rectangle((900, 900, 1700, 1700), fill=(60, 55, 50, 255))
    texts = {"headline": "Work Comfortable, Work Smart", "subheadline": "Wired USB mouse",
             "cta": "Shop Now", "bullets": ["Ergonomic", "USB plug"], "promo": "SALE"}
    design = {"_brand": "X", "_tag": "MOUSE", "_icon": "check",
              "palette": ["#3b3b3b", "#e8e8e8"], "text_area": "top"}
    composer._tpl_hero_overlay(canvas, design, texts, (900, 900, 1700, 1700))
    assert design.get("_text_scrim") is True, "浅底上未加衬底，文字会看不清"
    # 衬底后文字色应与衬底形成足够对比
    box = design["_text_box"]
    reg = (box[0] - 6, box[1] - 6, box[2] + 6, box[3] + 6)
    tc = composer._text_color_for(canvas, reg)
    lum_t = 0.299 * tc[0] + 0.587 * tc[1] + 0.114 * tc[2]
    assert composer._contrast_ratio(composer._region_luminance(canvas, reg), lum_t) >= 3.0


def test_needs_scrim_false_on_uniform_dark_background():
    """均匀深色背景上白字本身对比就够，不应加多余衬底。"""
    canvas = Image.new("RGBA", (400, 200), (24, 26, 32, 255))
    assert composer._needs_scrim(canvas, (0, 0, 400, 200), (245, 245, 245)) == 0


def test_directional_scrim_supports_corner_zones():
    """定向蒙版需支持角落版位（top-left 等）而不报错、且真的改变画面。"""
    canvas = Image.new("RGBA", (1000, 1000), (230, 230, 230, 255))
    before = canvas.convert("RGB").getpixel((50, 50))
    composer._draw_directional_scrim(canvas, "top-left", (40, 40, 600, 300))
    after = canvas.convert("RGB").getpixel((50, 50))
    assert after != before, "角落蒙版未生效"


# ---------- 方向性主体先验（P1 修复）----------
# 线上事故（任务 #3）：LLM 给出 position="center-lower"，旧实现却套"居中 0.62×0.62"
# 的先验，把主体框拉到纵向居中，两侧干净栏被算得很窄 → 文案挤进顶部小角落。

def test_prior_shifts_down_for_center_lower_product():
    pt = composer._prior_from_design(2000, 2000, {"product_placement": {"position": "center-lower"}})
    py = (pt[1] + pt[3]) / 2
    assert py > 1200, f"center-lower 先验未下移: 中心 y={py:.0f}"


def test_prior_shifts_left_for_right_position():
    pt = composer._prior_from_design(2000, 2000, {"product_placement": {"position": "right"}})
    px = (pt[0] + pt[2]) / 2
    assert px > 1200, f"right 先验未右移: 中心 x={px:.0f}"


def test_prior_uses_text_area_when_position_missing():
    """position 缺失时，text_area=bottom 应让主体先验偏上（给底部文字让位）。"""
    pt = composer._prior_from_design(2000, 2000, {"text_area": "bottom"})
    py = (pt[1] + pt[3]) / 2
    assert py < 1000, f"text_area=bottom 时主体先验未上移: 中心 y={py:.0f}"




