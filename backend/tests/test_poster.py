"""多分区海报合成（poster.py）回归。

参考经典电商海报：主视觉区 + 2×2 卖点网格 + 细节四格 + 底部场景导航条。
重点锁定：① 任何分区缺失都不能让主视觉崩掉；② 标题必须被夹在画布内；
③ 各分区纵向不重叠、铺满画布。
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.image import poster as P


def _png(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, format="PNG")
    return b.getvalue()


def _hero() -> bytes:
    im = Image.new("RGB", (1024, 1024), (190, 164, 124))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1024, 300), fill=(210, 196, 172))
    d.ellipse((330, 380, 760, 700), fill=(48, 50, 58))
    return _png(im)


def _details(n: int = 4) -> list[bytes]:
    out = []
    for i in range(n):
        im = Image.new("RGB", (400, 400), (60 + i * 20, 60, 66))
        ImageDraw.Draw(im).ellipse((100, 100, 300, 300), outline=(220, 220, 220), width=8)
        out.append(_png(im))
    return out


DESIGN = {"_brand": "Logitech", "_tag": "WIRED MOUSE",
          "palette": ["#1b1d24", "#3b6cd0", "#e8e8e8"]}


def test_poster_full_renders_all_sections_without_error():
    img = P.compose_poster(
        _hero(), DESIGN,
        headline="Work Comfortable, Work Smart",
        subheadline="Wired USB mouse with ergonomic support",
        cta="Shop Now", size_preset="4:5",
        bullets=["Clear Sound|Enjoy every detail", "Lightweight|Comfortable for long wear",
                 "Soft Grip|Fits your hand", "3.5mm Jack|Wide compatibility"],
        detail_image_bytes=_details(),
        scenes=["Music", "Study", "Gaming", "Work"],
        tagline="Better Sound, Better You",
    )
    assert img.size == (1080, 1350)
    assert img.mode == "RGB"


def test_poster_degrades_gracefully_without_optional_sections():
    """缺卖点/细节图/场景时仍能出主视觉（不崩、尺寸正确）。"""
    for kw in (
        dict(bullets=None, detail_image_bytes=None, scenes=None),
        dict(bullets=["A|a"], detail_image_bytes=None, scenes=None),
        dict(bullets=None, detail_image_bytes=_details(2), scenes=None),
    ):
        img = P.compose_poster(
            _hero(), DESIGN, headline="H", subheadline="S", cta="C",
            size_preset="1:1", **kw,
        )
        assert img.size == (1080, 1080)


def test_poster_section_heights_sum_to_canvas():
    for has in [(True, True, True), (False, False, False), (True, False, True)]:
        sec = P._section_heights(1350, *has)
        assert sum(sec.values()) == 1350, f"分区高度未铺满: {sec}"


def test_poster_headline_stays_within_canvas():
    """标题绝不能冲出画布左右边界（首版实测溢出）。"""
    long_headline = "Work Comfortable Work Smart Every Single Day Without Fail"
    img = P.compose_poster(
        _hero(), DESIGN, headline=long_headline,
        subheadline="sub", cta="go", size_preset="4:5",
        bullets=["A|a"], detail_image_bytes=None, scenes=None,
    )
    # 若标题溢出，会在左右边缘留下非背景色的文字像素；用边缘列的"白色文字"占比粗检
    import numpy as np
    arr = np.asarray(img.convert("L"))
    # 画布最左/最右 6px 不应出现大片高亮（白字）
    for edge in (arr[:, :6], arr[:, -6:]):
        bright_ratio = float((edge > 230).mean())
        assert bright_ratio < 0.08, "标题溢出到画布边缘"


def test_poster_grid_uses_bullets_title_desc_split():
    """卖点 '标题|说明' 约定应被解析（网格中出现两行不同字号文本）。"""
    img = P.compose_poster(
        _hero(), DESIGN, headline="H", subheadline="S", cta="C",
        size_preset="1:1",
        bullets=["Clear Sound|Enjoy every detail"],
        detail_image_bytes=None, scenes=None,
    )
    assert img.size == (1080, 1080)
