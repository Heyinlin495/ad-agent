"""电商平台「多分区宣传海报」合成。

参考经典电商详情/主图海报的结构，把一张广告图拆成 5 个纵向分区：

    ┌─────────────────────────────┐
    │  ① 主视觉区（hero）           │  深色渐变蒙版 + 大标题/副标题/品牌
    │     AI 场景图 + 文案          │  产品悬浮/陈列于场景
    ├──────────────┬──────────────┤
    │ ② 卖点网格 2×2（图标+标题）   │  圆形图标 + 粗体小标题 + 说明
    ├──────┬──────┬──────┬───────┤
    │ ③ 细节四格（AI 特写 + 标题）  │  AI 生成的 4 张产品特写
    ├─────────────────────────────┤
    │ ④ 底部场景导航条（深色）       │  图标 + 场景词 + 标语
    └─────────────────────────────┘

为什么单独成模块：`composer.py` 关注"单张图的文字排版（避让主体）"，而海报是
"多张素材 + 分区版式"的**版面编排**，职责不同。这里只做编排，文字渲染/避让复用
composer 的既有能力。

分区可按素材可得性降级：缺细节图 → 不画 ③；缺卖点 → 不画 ②；缺场景 → 不画 ④。
任何分区缺失都不能让主视觉区崩掉——主视觉区是海报的绝对核心。
"""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageDraw

from app.image.fonts import load_font
from app.image import composer as C


# ---------------------------------------------------------------- 字号基准
# 🚨 字号必须锚定**全图画布高度**，而不是所属分局的高度。
# 历史事故：各分局曾按 `ch * 0.24`（ch=单元格高）/ `cap_h * 0.42`（cap_h=标题带高）
# 计算字号。这些基数本身只占全图 8%~20%，再乘一个小系数后，正文字号只剩
# 8~10px（1080 画布，占画布高 0.9%），在手机上完全不可读。
# 现在统一为 `H * 系数`，使字号只随画布缩放、不随分局被挤压。
#
# 各系数按电商主图的移动端可读性下限校准：正文不小于画布高的 2%
# （1080 下约 22px）。经验值参考：标题 3.0%、正文 2.2%、辅助 2.4%。
_FS_GRID_TITLE = 0.030     # 卖点网格 · 标题
_FS_GRID_DESC = 0.022      # 卖点网格 · 说明
_FS_DETAIL_CAP = 0.024     # 细节四格 · 图下标题
_FS_FOOTER = 0.022         # 底部场景条 · 场景词
_FS_EYEBROW = 0.022        # 主视觉 · 眉题 / 品牌行
_FS_HERO_TITLE = 0.130     # 主视觉 · 主标题基准（自适应放大起点）
_FS_HERO_TITLE_MAX = 0.170  # 主视觉 · 主标题放大上限
_FS_HERO_SUB = 0.050       # 主视觉 · 副标题

# 字号地板（绝对像素）：任何情况下不低于此值，避免极小画布下彻底不可读
_MIN_FONT_PX = 14


def _fs(canvas_h: int, ratio: float) -> int:
    """按画布高度求字号，并套用绝对地板。"""
    return max(_MIN_FONT_PX, int(round(canvas_h * ratio)))


def _line_h(font: Any, ratio: float = 1.18) -> int:
    """按字体自身度量求**行高**（walk line height）。

    🚨 不能用 `font.size * ratio`。PIL 的 `font.size` 是 point size，而
    `truetype` 字体的 `ascent + descent` 通常大于 point size（实测 30px 字体
    为 32+8=40px），且带下伸部的字母（g/y/p）会继续往下探。旧版用 `font.size`
    估行高，导致**相邻两行文字纵向重叠**（实测网格区"标题"压在"说明"上）。
    """
    try:
        asc, desc = font.getmetrics()
        return int((asc + desc) * ratio)
    except Exception:  # noqa: BLE001  默认位图字体无 getmetrics
        return int(getattr(font, "size", 12) * 1.3)


def _fit_line(text: str, font: Any, max_w: int, max_lines: int) -> list[str]:
    """把 `text` 换行并截断到 `max_lines` 行，**超出的部分丢掉**。

    🚨 旧版用切片 `[...][:max_lines]` 截断，但渲染循环却按 `font.size` 逐行推 y，
    于是"被截掉的行"的位置仍是空的，视觉上像是漏了一行。这里统一在换行阶段收口。
    """
    lines = C._wrap_text(text, font, max_w)
    return lines[:max_lines]


# ---------------------------------------------------------------- 配色
def _palette(design: dict[str, Any]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """从视觉方案 palette 推 (深色, 强调色)。

    海报需要一对"深色底 + 亮强调"用于分区底色与图标。palette 缺失时给一套
    通用深灰/品牌紫兜底，保证任何方案都能出海报。
    """
    dark = (28, 30, 38)
    accent = (59, 108, 208)
    cols: list[tuple[int, int, int]] = []
    for c in design.get("palette") or []:
        try:
            cols.append(C._hex_to_rgb(str(c)))
        except (ValueError, TypeError):
            continue
    if cols:
        # 最深的一色作底、饱和度最高的一色作强调
        dark = min(cols, key=lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2])
        accent = max(cols, key=lambda c: max(c) - min(c))
    # 底若不够深，压暗，保证白字可读
    if 0.299 * dark[0] + 0.587 * dark[1] + 0.114 * dark[2] > 150:
        dark = tuple(int(v * 0.45) for v in dark)
    return dark, accent


def _text_on(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return (245, 245, 248) if lum < 140 else (26, 26, 30)


def _muted(fg: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(int(c * 0.62 + 180 * 0.38) for c in fg)


# ---------------------------------------------------------------- 分区高度
def _section_heights(h: int, has_grid: bool, has_details: bool, has_footer: bool) -> dict[str, int]:
    """按"哪些分区存在"分配纵向空间。

    权重经**可读性校准**：旧版 hero .56 / grid .20 / details .18 / footer .055 把
    底栏压到全图 5.5%（1080 下仅 59px），连"图标 + 场景词"都排不开。现改为
    hero .52 / grid .23 / details .19 / footer .075，把纵向空间从主视觉匀给三个
    文字分区（主视觉铺满即可，多出的高度对观感贡献很小，但少了就是"字挤在一起"）。
    另外底栏单独 clamp 一个**最低高度**，防止素材降级时被进一步压扁。
    """
    weights = {"hero": 0.50}
    if has_grid:
        weights["grid"] = 0.26
    if has_details:
        weights["details"] = 0.185
    if has_footer:
        weights["footer"] = 0.075
    total = sum(weights.values())
    out: dict[str, int] = {}
    acc = 0
    keys = list(weights.keys())
    for i, k in enumerate(keys):
        if i == len(keys) - 1:
            out[k] = h - acc
        else:
            v = int(h * weights[k] / total)
            out[k] = v
            acc += v
    # 底栏最低高度：图标（h*0.20 半径）与场景词（字号）纵向叠放需要约 0.07h 才不挤。
    if has_footer:
        floor = int(h * 0.07)
        if out["footer"] < floor:
            deficit = floor - out["footer"]
            out["footer"] = floor
            # 从主视觉区扣除（主视觉吸收能力最强）
            out["hero"] = max(int(h * 0.40), out["hero"] - deficit)
            # 若仍不足以补齐，继续从细节带扣
            still = h - sum(out.values())
            if still > 0:
                out["details"] = max(int(h * 0.14), out["details"] - still)
    return out


# ---------------------------------------------------------------- ① 主视觉区
def _draw_hero_section(
    poster: Image.Image,
    hero_img: Image.Image,
    design: dict[str, Any],
    headline: str,
    subheadline: str,
    brand: str,
    eyebrow: str,
    box: tuple[int, int, int, int],
) -> None:
    """把 AI 场景图贴进主视觉区，并叠加"深色渐变蒙版 + 文案"。

    文案优先排**底部**（参考海报的经典版式：大标题压在画面下方的深色渐变上），
    这样既保证对比度，又不会挡住画面中央的产品。
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0

    # 场景图按 cover 适配主视觉区
    hero = hero_img.convert("RGBA")
    sw, sh = hero.size
    scale = max(w / sw, h / sh) if sw and sh else 1
    nw, nh = max(w, int(sw * scale + 0.5)), max(h, int(sh * scale + 0.5))
    hero = hero.resize((nw, nh), Image.LANCZOS)
    hero = hero.crop(((nw - w) // 2, (nh - h) // 2, (nw - w) // 2 + w, (nh - h) // 2 + h))
    poster.alpha_composite(hero, (x0, y0))

    # 底部深色渐变蒙版（自下而上 alpha 递减），保证文案对比度。
    # 覆盖范围要盖住整块文案区（标题可能占 3 行），故取 0.72h。
    scrim_h = int(h * 0.74)
    overlay = Image.new("RGBA", (w, scrim_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    steps = 96
    for i in range(steps):
        t = i / steps
        a = int(238 * (t ** 1.25))       # 底部最实，向上快速衰减
        yy = int(scrim_h * t)
        od.rectangle([0, yy, w, yy + int(scrim_h / steps) + 1], fill=(6, 8, 14, a))
    poster.alpha_composite(overlay, (x0, y0 + h - scrim_h))

    draw = ImageDraw.Draw(poster)
    tc = (246, 246, 250)
    pad = int(w * 0.055)
    tx = x0 + pad
    max_w = w - 2 * pad
    # 字号锚定**画布总高**（poster.size[1]），而非主视觉区高度——这样主视觉区
    # 高度被其它分区挤压时，标题不会跟着缩水（历史事故见文件顶部说明）。
    canvas_h = poster.size[1]

    def _fit_font(text: str, start_px: int, bold: bool, max_lines: int,
                  max_px: int | None = None) -> Any:
        """在"行数 ≤ max_lines 且每行不溢出"的前提下，取**尽可能大**的字号。

        旧实现是**单向收缩**（从 base_px 只往下乘 0.94）：短标题因为一行就放得下，
        根本不会触发收缩，于是永远停在基准值上——"字偏小"的观感正来源于此。
        新实现先向上探测放大（×1.06，封顶 max_px），越过临界点后回退一档，
        让短标题自动撑满可用宽度，长标题再逐级收缩到能放下。
        """
        ceiling = max_px or start_px
        # 阶段一：向上探测，记录最后一个"放得下"的字号
        best = None
        px = float(start_px)
        while px <= ceiling * 1.0001:
            f = load_font(max(_MIN_FONT_PX, int(px)), bold=bold)
            lines = C._wrap_text_balanced(text, f, max_w)
            if len(lines) <= max_lines and all(f.getlength(ln) <= max_w for ln in lines):
                best = f
                px *= 1.06
            else:
                break
        if best is not None:
            return best
        # 阶段二：基准值都放不下 → 向下收缩
        px = float(start_px)
        for _ in range(30):
            f = load_font(max(_MIN_FONT_PX, int(px)), bold=bold)
            lines = C._wrap_text_balanced(text, f, max_w)
            if len(lines) <= max_lines and all(f.getlength(ln) <= max_w for ln in lines):
                return f
            px *= 0.94
        return load_font(max(_MIN_FONT_PX, int(px)), bold=bold)

    # 自底向上堆叠（贴主视觉区底部）：副标题 → 主标题 → 眉题
    # 行高一律用字体真实度量 `_line_h`，否则大字号下相邻行会纵向重叠。
    #
    # 🚨 纵向容量是**硬约束**：hero 区高度固定，而"眉题 + 品牌行 + 标题 + 副标题"
    # 的总高度可能超出它（实测：3 行超长标题 + 2 行超长副标题 → 文字溢出到网格区、
    # 顶部露出黑边）。因此这里先算出各块的"高度预算"，超预算就按**优先级**压缩：
    # 副标题（1 行）→ 标题（2 行）→ 眉题（字号下限）。绝不能靠"整体平移"补救。
    y = y1 - pad

    label_font = load_font(_fs(canvas_h, _FS_EYEBROW), bold=True)
    brand_font = load_font(_fs(canvas_h, _FS_EYEBROW), bold=True)
    # 顶部预留：品牌行（若存在）占其行高 + 顶部边距
    top_reserve = y0 + int(h * 0.045) + (_line_h(brand_font) if brand else 0)
    # 眉题占位
    eyebrow_h = int(_line_h(label_font) * 1.6) if eyebrow else 0
    # 可供标题 + 副标题使用的总高度
    text_budget = max(0, (y1 - pad) - top_reserve - eyebrow_h)

    if subheadline:
        font_s = _fit_font(subheadline, _fs(canvas_h, _FS_HERO_SUB), False, 2)
        lines = _fit_line(subheadline, font_s, max_w, 2)
        # 副标题是**次要信息**：若与标题相加超出纵向预算，先牺牲它（缩到 1 行）
        if len(lines) * _line_h(font_s) > text_budget * 0.45:
            lines = _fit_line(subheadline, font_s, max_w, 1)
        y -= len(lines) * _line_h(font_s)
        yy = y
        for ln in lines:
            draw.text((tx, yy), ln, font=font_s, fill=_muted(tc))
            yy += _line_h(font_s)

    if headline:
        font_h = _fit_font(
            headline,
            _fs(canvas_h, _FS_HERO_TITLE),
            True,
            3,
            max_px=_fs(canvas_h, _FS_HERO_TITLE_MAX),
        )
        lines = _fit_line(headline, font_h, max_w, 3)
        # 标题也不得撑破预算：超出就把上限从 3 行收到 2 行，并逐级缩字号
        sub_h = (len(_fit_line(subheadline, font_s, max_w, 1)) * _line_h(font_s)
                 if subheadline else 0)
        avail_for_head = text_budget - sub_h
        for _ in range(20):
            if len(lines) * _line_h(font_h) <= avail_for_head or font_h.size <= _MIN_FONT_PX:
                break
            font_h = load_font(max(_MIN_FONT_PX, int(font_h.size * 0.92)), bold=True)
            lines = _fit_line(headline, font_h, max_w, 3)
        y -= len(lines) * _line_h(font_h)
        yy = y
        for ln in lines:
            draw.text((tx, yy), ln, font=font_h, fill=tc)
            yy += _line_h(font_h)

    if eyebrow:
        y -= eyebrow_h
        # 眉题不得高于"品牌行下方"（否则两者重叠）
        if y < top_reserve:
            y = top_reserve
        # 按眉题所在处的实际背景亮度选字色，避免"浅字压浅底"
        e_lum = C._region_luminance(
            poster,
            (int(tx), int(y), int(tx + max_w), int(y + _line_h(label_font))),
        )
        e_col = _muted(tc) if e_lum < 140 else (28, 30, 36)
        draw.text((tx, y), eyebrow.upper(), font=label_font, fill=e_col)
        # 眉题右侧的短横线（参考海报的分隔线）
        lw = int(label_font.getlength(eyebrow.upper()))
        line_x = tx + lw + int(label_font.size * 0.7)
        ly = y + int(_line_h(label_font) * 0.45)
        draw.rectangle([line_x, ly, min(x1 - pad, line_x + int(w * 0.14)), ly + max(2, int(canvas_h * 0.005))],
                       fill=e_col)

    # 顶部左侧品牌行。
    # 🚨 不要再回退到 `_tag`：`_tag`（category_tag）已作为 `eyebrow` 参数传给本函数、
    # 渲染在文案区上方，若此处再用同一个字符串，同一张图会出现两处 "AUDIO"
    # （品类标签语义上是"右上角标签"，不该重复出现在标题区顶部）。
    # 品牌为空时就**不画**这一行，把空间让给文案。
    if brand:
        font_b = load_font(_fs(canvas_h, _FS_EYEBROW), bold=True)
        b_y0 = y0 + int(h * 0.045)
        # 标签行按背景亮度选字色，避免"白字压浅底"看不见
        bl = C._region_luminance(poster, (int(tx), int(b_y0), int(tx + max_w), int(b_y0 + font_b.size)))
        b_col = (246, 246, 250) if bl < 140 else (28, 30, 36)
        draw.text((tx, b_y0), brand[:30], font=font_b, fill=b_col)


def _shrink_to_fit(
    text: str,
    start_px: int,
    max_w: int,
    max_lines: int,
    bold: bool = False,
    min_px: int = _MIN_FONT_PX,
) -> Any:
    """从 `start_px` 起逐级缩小字号，直到文本能装进 `max_lines` 行且每行不溢出。

    用于网格/细节带这类**单行本应放下、但个别长词会撑破栏宽**的场景：
    旧版直接按原字号换行再截断，长标题会被拦腰砍断（实测"A"+"Accessories"残行）。
    """
    px = float(start_px)
    for _ in range(24):
        f = load_font(max(min_px, int(round(px))), bold=bold)
        lines = C._wrap_text(text, f, max_w)
        if len(lines) <= max_lines and all(f.getlength(ln) <= max_w for ln in lines):
            return f
        px *= 0.94
    return load_font(max(min_px, int(round(px))), bold=bold)


def _fit_block(
    segments: list[str],
    start_px_list: list[int],
    max_w: int,
    max_lines_list: list[int],
    avail_h: int,
    bold_list: list[bool] | None = None,
) -> tuple[list[Any], list[list[str]]]:
    """同时满足**宽度**与**高度**约束，为一组文本段求字号与换行结果。

    🚨 为什么需要它：`_shrink_to_fit` 只管宽度（每行放得下）和行数上限，
    **完全不看纵向可用高度**。当某个分区的文字段较多（如网格 = 标题 2 行 + 说明 2 行）
    时，各段各自算出来的行高之和会远超单元格高度，导致**相邻单元格文字互相压叠**
    （实测：需要 176px，可用仅 76px，第 1、3 格文字重叠成一团）。

    这里对整组文本做一个**缩放因子**搜索：任一"宽度/行数/总高度"约束不满足就整体
    缩 6%，直到全部满足或触底 `_MIN_FONT_PX`。
    """
    bolds = bold_list or [False] * len(segments)
    k = 1.0
    for _ in range(30):
        fonts: list[Any] = []
        all_lines: list[list[str]] = []
        ok = True
        total_h = 0
        for seg, base, ml, bd in zip(segments, start_px_list, max_lines_list, bolds):
            px = max(_MIN_FONT_PX, int(round(base * k)))
            f = load_font(px, bold=bd)
            lns = C._wrap_text(seg, f, max_w) if seg else []
            if len(lns) > ml or any(f.getlength(ln) > max_w for ln in lns):
                ok = False
                break
            fonts.append(f)
            all_lines.append(lns)
            total_h += len(lns) * _line_h(f)
        if ok and total_h <= avail_h:
            return fonts, all_lines
        k *= 0.94
    # 触底兜底：按最小字号重新生成一次（保证调用方能拿到可渲染的结果）
    fonts = [load_font(max(_MIN_FONT_PX, int(round(b * k))), bold=bd)
             for b, bd in zip(start_px_list, bolds)]
    all_lines = [C._wrap_text(seg, f, max_w) if seg else []
                 for seg, f in zip(segments, fonts)]
    return fonts, all_lines


# ---------------------------------------------------------------- ② 卖点网格
def _draw_check(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    r: int,
    color: tuple[int, int, int],
) -> None:
    """在圆心 (cx, cy)、半径 r 的圆内画一个对勾记号。

    抽成公共函数是为了让**卖点网格**与**底部场景条**共用同一图标语汇——
    旧版底栏只画了空圆环，没有任何记号，看起来像"没画完"（视觉不统一）。
    """
    cw2 = max(2, int(r * 0.20))
    pts = [(-0.38, 0.0), (-0.10, 0.28), (0.40, -0.30)]
    coords = [(cx + int(px * r), cy + int(py * r)) for px, py in pts]
    draw.line(coords, fill=color, width=cw2, joint="curve")


def _draw_sellpoint_grid(
    poster: Image.Image,
    bullets: list[str],
    box: tuple[int, int, int, int],
    dark: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> None:
    """2×2 卖点网格：圆形描边图标 + 粗体小标题 + 一行说明。

    卖点文案约定为 `标题|说明`（缺省只有标题）。参考海报的 2×2 是这类
    电商海报最有辨识力的元素之一。

    🚨 两处历史事故（2026-10-11 修复）：
    ① 字号曾按单元格高 `ch*0.24` 计算，实测只有 8px → 改锚定画布总高；
    ② 各格**独立**算字号，长文本的格子行数多、行高之和超出单元格，
       于是文字**溢出到下一行单元格并压叠**（实测需要 176px、可用仅 76px）。
       现改为：**全网格统一一组字号**（视觉整齐），且对"标题 2 行 + 说明 2 行"
       的总高度做硬约束，装不下就整体缩字号。
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    if not bullets:
        return
    draw = ImageDraw.Draw(poster)
    fg = _text_on(dark)
    pad_x = int(w * 0.05)
    pad_y = int(h * 0.05)
    cols, rows = 2, 2
    cell_w = (w - 2 * pad_x) // cols
    cell_h = (h - 2 * pad_y) // rows

    # 🚨 图标不能太占地方：2×2 网格在 1:1 画布上每格仅 ~486×127px，若图标按
    # "短边 13%" 取半径（~16px、直径 32px），留给文字的高度会被压到 65px，
    # 连"标题 + 说明"都放不下 → 高度约束反过来把字号压到 17px（低于可读地板）。
    # 这里把图标半径压到单元格高的 1/8（约 14px），且直径不超过高度的 22%。
    max_icon_r = max(5, min(int(min(cell_w, cell_h) * 0.10), int(cell_h * 0.11)))
    # 文字可用高度 = 单元格高 - 图标直径 - 上下呼吸位
    avail_h = max(24, cell_h - 2 * max_icon_r - int(cell_h * 0.10))

    # 统一求一组字号：取"全网格最长的标题/说明"作为约束（保证等大且都不越界）
    items: list[tuple[str, str]] = []
    for b in bullets[:4]:
        parts = [p.strip() for p in str(b).split("|", 1)]
        items.append((parts[0], parts[1] if len(parts) > 1 else ""))
    longest_t = max((t for t, _ in items), key=len, default="")
    longest_d = max((d for _, d in items), key=len, default="")

    canvas_h = poster.size[1]
    # 说明文字有左侧缩进（对齐图标），其可用宽度略窄于标题
    desc_indent = int(max_icon_r * 0.4)
    title_w = cell_w - max_icon_r
    desc_w = cell_w - desc_indent
    fonts, _ = _fit_block(
        [longest_t, longest_d],
        [_fs(canvas_h, _FS_GRID_TITLE), _fs(canvas_h, _FS_GRID_DESC)],
        title_w,
        [2, 2],
        avail_h,
        [True, False],
    )
    font_t, font_d = fonts[0], fonts[1]

    for i, (title, desc) in enumerate(items):
        r, c = divmod(i, cols)
        cx = x0 + pad_x + c * cell_w
        cy = y0 + pad_y + r * cell_h
        icon_cx = cx + max_icon_r
        icon_cy = cy + max_icon_r
        draw.ellipse(
            [icon_cx - max_icon_r, icon_cy - max_icon_r,
             icon_cx + max_icon_r, icon_cy + max_icon_r],
            outline=accent, width=max(2, int(max_icon_r * 0.22)),
        )
        _draw_check(draw, icon_cx, icon_cy, max_icon_r, accent)

        t_lines = _fit_line(title, font_t, title_w, 2)
        d_lines = _fit_line(desc, font_d, desc_w, 2) if desc else []
        # 纵向：图标 → 标题 → 说明；整体在单元格剩余空间内垂直居中
        text_h = len(t_lines) * _line_h(font_t) + len(d_lines) * _line_h(font_d)
        top = icon_cy + max_icon_r
        avail_below = (cy + cell_h) - top
        ty = top + max(0, (avail_below - text_h) // 2)
        for ln in t_lines:
            draw.text((cx, ty), ln, font=font_t, fill=fg)
            ty += _line_h(font_t)
        for ln in d_lines:
            draw.text((cx + desc_indent, ty), ln, font=font_d, fill=_muted(fg))
            ty += _line_h(font_d)


# ---------------------------------------------------------------- ③ 细节四格
def _draw_detail_strip(
    poster: Image.Image,
    detail_imgs: list[Image.Image],
    captions: list[str],
    box: tuple[int, int, int, int],
    dark: tuple[int, int, int],
) -> None:
    """细节四格：4 张 AI 特写等宽排列，下方各配一行标题。

    参考海报的"细节特写带"——用产品的真实细节建立可信度。
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    if not detail_imgs:
        return
    n = min(4, len(detail_imgs))
    draw = ImageDraw.Draw(poster)
    fg = _text_on(dark)
    gap = int(w * 0.018)
    pad_x = int(w * 0.03)
    pad_y = int(h * 0.08)
    cell_w = (w - 2 * pad_x - gap * (n - 1)) // n
    # 标题带高度按**画布字号 + 真实行高**反推，而不是按分区高度的固定比例——
    # 否则字号调大后标题会顶到图片下沿（旧版 cap_h=h*0.22 时标题会压到图上）。
    cap_font_px = _fs(poster.size[1], _FS_DETAIL_CAP)
    cap_line = _line_h(load_font(cap_font_px, bold=True))
    cap_h = cap_line + int(h * 0.10)
    img_h = h - 2 * pad_y - cap_h

    for i in range(n):
        cx = x0 + pad_x + i * (cell_w + gap)
        cy = y0 + pad_y
        cell = detail_imgs[i].convert("RGB")
        sw, sh = cell.size
        sc = max(cell_w / sw, img_h / sh) if sw and sh else 1
        nw, nh = max(cell_w, int(sw * sc + 0.5)), max(img_h, int(sh * sc + 0.5))
        cell = cell.resize((nw, nh), Image.LANCZOS)
        cell = cell.crop(((nw - cell_w) // 2, (nh - img_h) // 2,
                          (nw - cell_w) // 2 + cell_w, (nh - img_h) // 2 + img_h))
        poster.paste(cell, (cx, cy))
        # 细节图下方标题（字号锚定画布总高；长标题自动缩到单行放得下）
        cap = captions[i] if i < len(captions) else ""
        if cap:
            font_c = _shrink_to_fit(cap, _fs(poster.size[1], _FS_DETAIL_CAP), cell_w, 1, bold=True)
            ty = cy + img_h + int(cap_h * 0.18)
            for ln in _fit_line(cap, font_c, cell_w, 1):
                lw = int(font_c.getlength(ln))
                draw.text((cx + (cell_w - lw) // 2, ty), ln, font=font_c, fill=fg)
                ty += _line_h(font_c)


# ---------------------------------------------------------------- ④ 底部导航条
def _draw_scene_footer(
    poster: Image.Image,
    scenes: list[str],
    tagline: str,
    box: tuple[int, int, int, int],
    dark: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> None:
    """底部深色场景导航条：图标 + 场景词（Music/Study/Gaming/Work 式）。

    字号锚定画布总高；圆环内补对勾记号，与卖点网格共用同一图标语汇。
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    draw = ImageDraw.Draw(poster)
    draw.rectangle([x0, y0, x1, y1], fill=dark)
    fg = _text_on(dark)
    n = len(scenes[:4])
    if n == 0:
        return
    seg = w / n
    # 场景词按本格可用宽度自动缩字号（旧版固定字号，长词如 "Office work" 会被右侧截断）
    gap = int(seg * 0.16)
    icon_r = max(6, int(h * 0.20))
    for i, sc in enumerate(scenes[:4]):
        cx = int(seg * (i + 0.5))
        icon_cx = cx - gap
        icon_cy = y0 + h // 2
        avail_w = int(seg * 0.5) - icon_r
        font_s = _shrink_to_fit(str(sc), _fs(poster.size[1], _FS_FOOTER), avail_w, 2)
        # 小圆点图标（+ 与网格同款对勾，保持图标族一致）
        draw.ellipse([icon_cx - icon_r, icon_cy - icon_r, icon_cx + icon_r, icon_cy + icon_r],
                     outline=accent, width=max(2, int(icon_r * 0.3)))
        _draw_check(draw, icon_cx, icon_cy, icon_r, accent)
        lines = _fit_line(str(sc), font_s, avail_w, 2)
        lh = _line_h(font_s)
        ty = icon_cy - (len(lines) * lh) // 2
        for ln in lines:
            draw.text((cx, ty), ln, font=font_s, fill=fg)
            ty += lh


# ---------------------------------------------------------------- 主入口
def compose_poster(
    hero_image_bytes: bytes,
    design: dict[str, Any],
    headline: str,
    subheadline: str,
    cta: str,
    size_preset: str,
    bullets: list[str] | None = None,
    detail_image_bytes: list[bytes] | None = None,
    detail_captions: list[str] | None = None,
    scenes: list[str] | None = None,
    tagline: str = "",
) -> Image.Image:
    """合成多分区海报，返回 PIL Image。

    分区按素材可得性自动裁剪：没有卖点就不画②、没有细节图就不画③、
    没有场景词就不画④。主视觉区恒存在。
    """
    size = C.resolve_size(size_preset)
    W, H = size
    dark, accent = _palette(design)

    hero_img = C._load_base_cover(hero_image_bytes, size, text_side="center", design=design)

    detail_imgs: list[Image.Image] = []
    for b in (detail_image_bytes or []):
        try:
            detail_imgs.append(Image.open(io.BytesIO(b)).convert("RGB"))
        except Exception:  # noqa: BLE001
            continue

    has_grid = bool(bullets)
    has_details = bool(detail_imgs)
    has_footer = bool(scenes)
    sec = _section_heights(H, has_grid, has_details, has_footer)

    poster = Image.new("RGBA", (W, H), dark + (255,))
    y = 0
    hero_box = (0, 0, W, sec["hero"])
    _draw_hero_section(
        poster, hero_img, design, headline, subheadline,
        (design.get("_brand") or "").strip(), (design.get("_tag") or "").strip(), hero_box,
    )
    y += sec["hero"]

    if has_grid:
        gbox = (0, y, W, y + sec["grid"])
        ImageDraw.Draw(poster).rectangle([gbox[0], gbox[1], gbox[2], gbox[3]], fill=dark)
        _draw_sellpoint_grid(poster, bullets or [], gbox, dark, accent)
        y += sec["grid"]

    if has_details:
        dbox = (0, y, W, y + sec["details"])
        ImageDraw.Draw(poster).rectangle([dbox[0], dbox[1], dbox[2], dbox[3]], fill=dark)
        # 细节带标题优先用调用方显式传入的 detail_captions（细节向文案）；
        # 缺失时回退到卖点标题（旧行为）。
        if detail_captions:
            caps = [str(c).strip() for c in detail_captions][:4]
        else:
            caps = [str(b).split("|")[0] for b in (bullets or [])][:4]
        _draw_detail_strip(poster, detail_imgs, caps, dbox, dark)
        y += sec["details"]

    if has_footer:
        fbox = (0, y, W, H)
        _draw_scene_footer(poster, scenes or [], tagline, fbox, dark, accent)

    return poster.convert("RGB")
