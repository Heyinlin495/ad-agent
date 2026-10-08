"""合成器版式硬约束：文字区不得压住产品主体（电视等宽扁产品尤其明显）。"""
from __future__ import annotations

import io

import pytest
from PIL import Image

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
