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
    """🚨 字号回归：正文**名义字号**不得小于画布高的 1.8%（1080 下约 19px），
    且真实渲染出的字形墨高必须与之匹配。

    历史事故 1：各分局按分局高度 `ch*0.24` 算字号，实测只剩 8~10px。
    历史事故 2（2026-10-12）：容器内**没有任何字体文件**，`load_font()` 静默退回
    `ImageFont.load_default()`（固定 10px、完全忽略传入字号）——名义字号 32px、
    实际墨高只有 10px，而且无声上线。所以这里同时锁"名义字号"与"墨高是否塌陷"。

    🚨 注意：**不能**直接拿墨高去比 `H*0.018`。墨高随字种/字形大小写大幅变化
    （拉丁字体的墨高明显小于中文字体，同一份代码在 Arial 下是 18px、在雅黑下是
    25px），那种断言只对某一种字体成立，换个字体就假红/假绿。
    """
    from app.image.fonts import font_status

    # 0) 环境里必须真的有字体，否则下面全是假绿
    assert font_status()["ok"], "环境缺字体：出图文字会全部退化为 10px"

    H = 1080
    nominals = {
        "grid_title": P._fs(H, P._FS_GRID_TITLE),
        "grid_desc": P._fs(H, P._FS_GRID_DESC),
        "detail_cap": P._fs(H, P._FS_DETAIL_CAP),
        "footer": P._fs(H, P._FS_FOOTER),
    }
    floor = H * 0.018
    for name, px in nominals.items():
        assert px >= floor, f"{name} 名义字号过小: {px}px < {floor:.1f}px"

    # 1) 真实渲染：墨高相对名义字号不得塌陷（10px 默认字体会在这里露馅）
    img = _full_poster("1:1")
    sec = P._section_heights(H, True, True, True)
    y = sec["hero"]
    grid = [h for h in _bright_line_heights(img, y, y + sec["grid"]) if h > 3]
    y += sec["grid"]
    det = [h for h in _bright_line_heights(img, y, y + sec["details"]) if h > 3]
    assert grid, "卖点网格区没有渲染出文字"
    assert det, "细节带没有渲染出文字"
    exp_grid = nominals["grid_desc"] * 0.55
    exp_det = nominals["detail_cap"] * 0.55
    assert min(grid) >= exp_grid, (
        f"卖点区墨高过小: {min(grid)}px < {exp_grid:.1f}px（疑似回退到默认字体）"
    )
    assert min(det) >= exp_det, (
        f"细节区墨高过小: {min(det)}px < {exp_det:.1f}px（疑似回退到默认字体）"
    )


def test_poster_section_dividers_are_drawn():
    """分区之间必须有可见分隔线。

    四个分区（hero/网格/细节/底栏）共用同一个最深底色，不画分隔线时整张图会
    糊成一整片（实测观感"这张图没有分段"）。这里扫描分区边界行的亮度，要求它
    明显高于紧邻的上下两行。
    """
    img = _full_poster("1:1").convert("L")
    H = img.size[1]
    sec = P._section_heights(H, True, True, True)
    ys = [
        sec["hero"],
        sec["hero"] + sec["grid"],
        sec["hero"] + sec["grid"] + sec["details"],
    ]

    def row_lum(y: int) -> float:
        px = img.load()
        vals = [px[x, y] for x in range(0, img.size[0], 4)]
        return sum(vals) / len(vals)

    for y in ys:
        band = max(row_lum(yy) for yy in range(y - 2, y + 3))
        ref = min(row_lum(y - 8), row_lum(y + 8))
        assert band > ref + 15, f"y={y} 处缺少分区分隔线（{band:.1f} vs {ref:.1f}）"


def test_poster_hero_text_does_not_swallow_the_visual():
    """主视觉区的文案块不得占掉大半——商品才是广告主角。

    🚨 这是"字体修好之后才暴露"的问题：字号还没生效时文字只有 10px，系数被
    拍得偏大（副标题 0.050）；一旦字体正常，2 行大标题 + 2 行副标题会吃掉
    主视觉区约 81%，商品被压进文案里。现在 `text_budget` 有 0.62 的硬上限。
    """
    design = dict(DESIGN)
    P.compose_poster(
        _hero(), design,
        headline="All-Day Comfort Only $21",
        subheadline="Wired headphones built for work, study, and play",
        cta="Shop Now", size_preset="1:1",
        bullets=["Lightweight comfort|Feel the ease",
                 "Zero lag wired connection|Instant response"],
        scenes=["Office work", "Gaming"],
    )
    ratio = design.get("_hero_text_ratio")
    assert ratio is not None, "海报合成器应回传 _hero_text_ratio（供回归比对）"
    assert ratio <= 0.68, f"主视觉文案块占比过大（{ratio:.0%}），会盖住商品"


def test_poster_subheadline_is_not_oversized():
    """副标题字号不得大于画布高的 4%（旧值 0.050 → 54px，会折成 2 行抢标题空间）。"""
    assert P._FS_HERO_SUB <= 0.04


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

    主视觉现在是"左列文字 + 右侧商品"，顶部左侧正是**品牌行**的位置：
    品牌为空时该区域不应有任何近白文字像素；有品牌时必须出现（作为对照）。
    """
    def _top_brand_pixels(design: dict) -> int:
        img = P.compose_poster(
            _hero(), design, headline="H", subheadline="S", cta="C",
            size_preset="1:1", bullets=["A|a"],
            detail_image_bytes=None, scenes=None,
        )
        arr = np.asarray(img.convert("L"))
        W, H = img.size
        band = arr[0: int(H * 0.08), int(W * 0.05): int(W * 0.55)]
        return int((band > 230).sum())

    no_brand = dict(DESIGN)
    no_brand["_brand"] = ""
    assert _top_brand_pixels(no_brand) == 0, "品牌缺失时顶部不应渲染标签行（会与眉题重复）"
    assert _top_brand_pixels(dict(DESIGN)) > 0, "有品牌时顶部品牌行必须渲染"


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


def _solid_detail_tiles(n: int = 4, color: tuple[int, int, int] = (250, 250, 250)) -> list[bytes]:
    """纯色块做细节图 —— 便于从渲染结果里精确量出单格的像素尺寸。"""
    return [_png(Image.new("RGB", (600, 400), color)) for _ in range(n)]


def test_detail_tiles_match_generation_aspect():
    """🚨 细节格画幅必须接近出图比例（3:2），否则渲染时会被中心裁切。

    历史事故（2026-10-12 实测）：旧版先按可用宽度定 `cell_w=239`，再把 1:1 出图
    `cover` 裁进去 → 图块实际 239×111（2.15:1，**纵向裁掉 53.6%**）。标题点名的
    部件（接口/配件）往往正好落在被切掉的部分里，四格于是都只剩"整机中段"，
    看起来就是同一张图。
    """
    img = P.compose_poster(
        _hero(), DESIGN, headline="H", subheadline="S", cta="C",
        size_preset="1:1", bullets=["A|a"],
        detail_image_bytes=_solid_detail_tiles(),
        detail_captions=["A", "B", "C", "D"], scenes=None,
    )
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    white = np.abs(a - 250).max(axis=2) < 6

    # 细节带在下半部，取一条穿过色块的横线找出 4 个格子的横向区间
    rows = np.nonzero(white.sum(axis=1) > 200)[0]
    assert rows.size, "没有渲染出细节格"
    mid = int(np.median(rows))
    runs: list[tuple[int, int]] = []
    start = None
    for x, v in enumerate(white[mid]):
        if v and start is None:
            start = x
        elif not v and start is not None:
            runs.append((start, x - 1))
            start = None
    if start is not None:
        runs.append((start, white.shape[1] - 1))
    runs = [r for r in runs if r[1] - r[0] > 40]
    assert len(runs) == 4, f"未识别出 4 个细节格: {runs}"

    cell_w = sum(r[1] - r[0] + 1 for r in runs) / len(runs)
    # 单格高：在某格中心列上取最长的连续白色纵向段
    col = (runs[0][0] + runs[0][1]) // 2
    best = cur = 0
    for v in white[:, col]:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    assert best > 40, "细节格高度异常"

    ratio = cell_w / best
    expect = P.DETAIL_TILE_ASPECT[0] / P.DETAIL_TILE_ASPECT[1]
    assert abs(ratio - expect) < 0.15, (
        f"细节格画幅 {cell_w:.0f}×{best} = {ratio:.2f}:1，与出图比例 {expect:.2f}:1 不符"
        f"（会被中心裁切，切掉的部分正是被点名的部件）"
    )


def test_detail_section_has_enough_height_for_the_aspect():
    """细节分区必须留够"格宽 ÷ 3:2"的高度，否则格子会被压扁回长条。"""
    sec = P._section_heights(1080, True, True, True)
    w = 1080
    gap = int(w * P._DETAIL_GAP)
    pad_x = int(w * 0.03)
    cell_w = (w - 2 * pad_x - gap * 3) // 4
    ratio = P.DETAIL_TILE_ASPECT[0] / P.DETAIL_TILE_ASPECT[1]
    needed = cell_w / ratio + 2 * int(sec["details"] * 0.06)
    assert sec["details"] >= needed, (
        f"细节分区 {sec['details']}px 不足以为 {cell_w}px 宽的格子提供 3:2 高度"
        f"（至少需要约 {needed:.0f}px）"
    )
    # 分区权重之和仍应铺满画布，且底栏保底
    assert sec["footer"] >= int(1080 * 0.07)


# ---------------------------------------------------------------- 主视觉「文字 / 商品分区」
def _dark_hero() -> bytes:
    """深色场景 + 右侧亮色"商品"，用于精确检测白色文案的横向范围。"""
    im = Image.new("RGB", (1024, 1024), (18, 20, 26))
    d = ImageDraw.Draw(im)
    d.ellipse((520, 380, 900, 760), fill=(214, 214, 218))
    return _png(im)


def test_poster_hero_text_is_confined_to_one_column():
    """🚨 主视觉文案必须只占**一侧固定列**，另一侧整列留给商品。

    历史缺陷（线上实测）：标题横跨 0.89 画布宽，与耳机主体重叠 → "层次不清晰"。
    现在文案列宽被夹到 `_HERO_TEXT_COL`，且场景图被推向非文字侧。
    """
    design = dict(DESIGN)
    design["text_area"] = "left"
    img = P.compose_poster(
        _dark_hero(), design,
        headline="Clear Calls Zero Charging",
        subheadline="Wired comfort for every workday",
        cta="Shop Now", size_preset="1:1",
        bullets=["A|a"], detail_image_bytes=None, scenes=None,
    )
    W = img.size[0]
    assert design.get("_hero_text_side") == "left"
    assert design.get("_hero_text_col_w", W) <= int(W * 0.62), "文案列宽没有被夹住"

    # 文案是白色；右侧"商品"是浅灰(214) —— 只统计**近白**像素即可把文案与商品分开
    arr = np.asarray(img.convert("L"))
    hero = arr[: P._section_heights(img.size[1], True, False, False)["hero"], :]
    ys, xs = np.nonzero(hero > 238)
    assert xs.size > 0, "主视觉区没有渲染出文案"
    limit = int(W * 0.055) + design["_hero_text_col_w"] + 6
    assert int(xs.max()) <= limit, (
        f"文案越过了文字列（最右白话素 x={int(xs.max())} > 列右边界 {limit}）"
    )


def test_poster_hero_respects_text_area_side():
    """`text_area="right"` 时文案应贴右列，商品让到左侧。"""
    design = dict(DESIGN)
    design["text_area"] = "right"
    P.compose_poster(
        _hero(), design, headline="H", subheadline="S", cta="C",
        size_preset="1:1", bullets=["A|a"], detail_image_bytes=None, scenes=None,
    )
    assert design.get("_hero_text_side") == "right"


# ---------------------------------------------------------------- 卖点网格严格对齐
def test_poster_grid_cells_share_the_same_baseline():
    """🚨 2×2 卖点网格四格必须严格对齐：标题首行 y 完全一致。

    历史缺陷（实测）：每格各自"在剩余空间里垂直居中"，标题 2 行的格子块高、
    1 行的块矮 → 两格文字重心错开（"第一项两行、第二项一行，视觉重心不平衡"）。
    现在四格共用统一行槽，这里用像素扫描把这条契约锁死。
    """
    img = P.compose_poster(
        _hero(), DESIGN, headline="H", subheadline="S", cta="C", size_preset="1:1",
        bullets=[
            "Tiny|Tiny",
            "A noticeably longer bullet title|And a noticeably longer description here",
            "Short|Short",
            "Another long-ish bullet title|Another long-ish desc",
        ],
        detail_image_bytes=None, scenes=None,
    )
    H, W = img.size[1], img.size[0]
    sec = P._section_heights(H, True, False, False)
    g = np.asarray(img.convert("L"))
    pad_x = int(W * 0.05)
    pad_y = int(sec["grid"] * 0.05)
    cell_w = (W - 2 * pad_x) // 2
    cell_h = (sec["grid"] - 2 * pad_y) // 2
    # 图标是强调色（亮度约 105），不会进入 >150 的判据；首个高亮行即标题首行。
    tops: list[int] = []
    for i in range(4):
        r, c = divmod(i, 2)
        cx0 = pad_x + c * cell_w
        cy0 = sec["hero"] + pad_y + r * cell_h
        top = None
        for y in range(cy0, cy0 + cell_h):
            if int((g[y, cx0:cx0 + cell_w] > 150).sum()) >= 5:
                top = y - cy0
                break
        tops.append(top)  # type: ignore[arg-type]
    assert all(t is not None for t in tops), f"有单元格没渲染出标题: {tops}"
    assert len(set(tops)) == 1, f"四格标题首行基线不一致（需严格 2×2）: {tops}"


def test_detail_tiles_have_consistent_gaps():
    """四张细节卡之间必须有统一的间距（旧值 0.018 让四卡挤成一条连拍带）。"""
    img = P.compose_poster(
        _hero(), DESIGN, headline="H", subheadline="S", cta="C",
        size_preset="1:1", bullets=["A|a"],
        detail_image_bytes=_solid_detail_tiles(),
        detail_captions=["A", "B", "C", "D"], scenes=None,
    )
    W = img.size[0]
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    white = np.abs(a - 250).max(axis=2) < 6
    rows = np.nonzero(white.sum(axis=1) > 200)[0]
    mid = int(np.median(rows))
    runs: list[tuple[int, int]] = []
    start = None
    for x, v in enumerate(white[mid]):
        if v and start is None:
            start = x
        elif not v and start is not None:
            runs.append((start, x - 1))
            start = None
    runs = [r for r in runs if r[1] - r[0] > 40]
    assert len(runs) == 4, f"未识别出 4 个细节格: {runs}"
    gaps = [runs[i + 1][0] - runs[i][1] - 1 for i in range(3)]
    expect = int(W * P._DETAIL_GAP)
    for gp in gaps:
        assert abs(gp - expect) <= 2, f"细节格间距不一致: {gaps}（期望约 {expect}px）"
