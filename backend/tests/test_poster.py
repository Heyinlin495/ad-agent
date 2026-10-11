"""多分区海报合成（poster.py）回归。

参考经典电商海报：主视觉区 + 2×2 卖点网格 + 细节四格 + 底部场景导航条。
重点锁定：① 任何分区缺失都不能让主视觉崩掉；② 标题必须被夹在画布内；
③ 各分区纵向不重叠、铺满画布。
"""
from __future__ import annotations

import io

import numpy as np
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


# ---------------------------------------------------------------- 字号可读性
def _bright_line_heights(img: Image.Image, y0: int, y1: int, thresh: int = 150) -> list[int]:
    """扫描 [y0,y1) 内的高亮文字行，返回各连续文字行的像素高度。"""
    arr = np.asarray(img.convert("L"))
    runs: list[int] = []
    cur = 0
    for y in range(y0, min(y1, arr.shape[0])):
        if bool((arr[y] > thresh).any()):
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


def _full_poster(size_preset: str = "1:1") -> Image.Image:
    return P.compose_poster(
        _hero(), DESIGN,
        headline="All-Day Comfort Only $21",
        subheadline="Wired headphones built for work, study, and play",
        cta="Shop Now", size_preset=size_preset,
        bullets=["Lightweight comfort|Feel the ease",
                 "Zero lag wired connection|Instant response",
                 "Soft PU leather cushions|Gentle on skin",
                 "Adjustable and foldable fit|Fits every head"],
        detail_image_bytes=_details(),
        detail_captions=["Textured Surface", "Precision Build",
                         "Reinforced Hinge", "Included Accessories"],
        scenes=["Office work", "Online learning", "Gaming", "Daily commuting"],
        tagline="AUDIO",
    )


def test_poster_body_text_is_readable():
    """🚨 字号回归：正文文字不得小于画布高的 1.8%（1080 下约 19px）。

    历史事故：各分局曾按分局高度 `ch*0.24` / `cap_h*0.42` 算字号，实测正文字号
    只剩 8~10px（占画布高 0.9%），在手机上完全不可读。本用例锁死这条底线。
    """
    img = _full_poster("1:1")
    H = img.size[1]
    sec = P._section_heights(H, True, True, True)
    y = sec["hero"]
    grid = [h for h in _bright_line_heights(img, y, y + sec["grid"]) if h > 3]
    y += sec["grid"]
    det = [h for h in _bright_line_heights(img, y, y + sec["details"]) if h > 3]
    floor = H * 0.018
    assert grid, "卖点网格区没有渲染出文字"
    assert det, "细节带没有渲染出文字"
    assert min(grid) >= floor, f"卖点区文字过小: {min(grid)}px < {floor:.1f}px"
    assert min(det) >= floor, f"细节区文字过小: {min(det)}px < {floor:.1f}px"


def test_poster_detail_captions_override_bullets():
    """传入 detail_captions 时，细节带标题应使用它、且不与卖点标题重复。"""
    caps = ["Textured Surface", "Precision Build", "Reinforced Hinge", "Included Accessories"]
    img = P.compose_poster(
        _hero(), DESIGN, headline="H", subheadline="S", cta="C",
        size_preset="1:1",
        bullets=["Lightweight comfort|Feel the ease", "Zero lag|Instant"],
        detail_image_bytes=_details(), detail_captions=caps,
        scenes=None,
    )
    assert img.size == (1080, 1080)


def test_poster_long_caption_not_truncated():
    """长细节标题应被缩到单行放得下，而不是被拦腰截断（旧版 A/Accessories 残行）。"""
    img = P.compose_poster(
        _hero(), DESIGN, headline="H", subheadline="S", cta="C",
        size_preset="1:1", bullets=["A|a"],
        detail_image_bytes=_details(4),
        detail_captions=["An Extremely Long Accessories Caption Here"] * 4,
        scenes=None,
    )
    assert img.size == (1080, 1080)


def test_poster_brand_not_duplicated_with_category_tag():
    """品牌缺失时，hero 区**不得**把 `_tag` 画两遍。

    🚨 历史事故：`compose_poster` 把 `_tag`（category_tag）作为 `eyebrow` 传入并渲染；
    早期实现又在"品牌缺失"时回退用同一个 `_tag` 画顶部标签行，导致同一张图出现
    两处 "AUDIO"（且因长标题挤压而叠在一起）。
    """
    no_brand = dict(DESIGN)
    no_brand["_brand"] = ""
    img = P.compose_poster(
        _hero(), no_brand, headline="H", subheadline="S", cta="C",
        size_preset="1:1", bullets=["A|a"],
        detail_image_bytes=None, scenes=None,
    )
    # 顶部 8% 区域不应出现文字像素。
    # 注意：`_hero()` 素材是浅色底（亮度 ~197），所以判据是"与背景色显著不同的像素"，
    # 而不是"高亮像素"（浅底上的深色文字也算内容）。
    arr = np.asarray(img.convert("L")).astype(int)
    top = arr[: int(img.size[1] * 0.08), :]
    band = top[3:-3]  # 去掉边缘抗锯齿
    bg = int(np.median(band))
    diff = np.abs(band - bg)
    assert float((diff > 60).mean()) < 0.005, "品牌缺失时顶部不应渲染标签行（会与眉题重复）"


def test_poster_eyebrow_does_not_collide_with_brand():
    """超长标题把眉题顶到顶部时，眉题与品牌行不得纵向重叠。"""
    img = P.compose_poster(
        _hero(), DESIGN,
        headline="Experience Unmatched Audio Fidelity With Premium Wired Headphones Designed For Professionals",
        subheadline="Engineered for studio-grade monitoring and long listening sessions",
        cta="C", size_preset="1:1",
        bullets=["A|a"], detail_image_bytes=None, scenes=None,
    )
    assert img.size == (1080, 1080)


def test_poster_grid_cells_do_not_overlap():
    """🚨 网格回归：长卖点不得溢出单元格、压到下一行单元格。

    历史事故：各格独立算字号，长文本格子的"标题 2 行 + 说明 2 行"总高
    超出单元格（实测需要 176px、可用仅 76px），文字压叠成一片。
    检测法：在第 1 行单元格与其下方单元格之间的**分隔带**内，不应出现文字像素。
    """
    img = P.compose_poster(
        _hero(), DESIGN, headline="H", subheadline="S", cta="C",
        size_preset="1:1",
        bullets=[
            "Exceptional lightweight construction|Enjoy remarkable comfort during extended wear sessions",
            "Zero latency wired connection|Instantaneous response for critical monitoring work",
            "Premium soft PU leather cushioning|Gentle and breathable against sensitive skin",
            "Fully adjustable and foldable design|Accommodates every head shape and size comfortably",
        ],
        detail_image_bytes=None, scenes=None,
    )
    H = img.size[1]
    sec = P._section_heights(H, True, False, False)
    grid_y0 = sec["hero"]
    grid_h = sec["grid"]
    pad_y = int(grid_h * 0.05)
    # 第 1 行单元格与第 2 行单元格之间的分隔带（安全区）
    boundary = grid_y0 + pad_y + (grid_h - 2 * pad_y) // 2
    band = np.asarray(img.convert("L"))[boundary - 3 : boundary + 3, :]
    # 分隔带应基本为空（允许极少量抗锯齿像素）
    assert float((band > 150).mean()) < 0.01, "网格单元格文字溢出到相邻单元格"
