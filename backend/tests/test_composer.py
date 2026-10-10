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
    """标题最后一行不得只剩 1~2 个字符（孤字行）——窄栏 + 大字号时的典型缺陷。

    用渲染时**实际采用的字号档与栏宽**重排同一标题；孤字是不可接受的排版硬伤，
    排序逻辑已把"无孤字"排在"字号更大"之前，本用例锁定该行为。
    """
    subject = (520, 260, 1960, 1880)
    design, _ = _overlay((2000, 2000), subject)
    assert "_text_scale" in design, "未记录采用的字号档，无法复核"
    canvas = _make_canvas((2000, 2000))
    # 以产出文字框宽度作为栏宽、以实际采用的字号档重排
    box = design["_text_box"]
    col_w = max(120, box[2] - box[0])
    texts = {"headline": "沉浸式听觉盛宴", "subheadline": "", "cta": "", "bullets": []}
    d = {"_brand": "", "palette": ["#1D1D1F"]}
    _, items, _ = composer._hero_text_layout(
        canvas, d, texts, 120, col_w, scale=float(design["_text_scale"]))
    checked = 0
    for it in items:
        if it.get("kind") == "text" and it.get("stroke") == 1:
            lines = composer._wrap_text(it["text"], it["font"], it["max_w"])
            checked += 1
            assert len(lines) < 2 or len(lines[-1].strip()) > 2, f"标题出现孤字行: {lines}"
    assert checked == 1, "未找到标题行，用例失效"

