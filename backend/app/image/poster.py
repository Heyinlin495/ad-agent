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
    """按"哪些分区存在"分配纵向空间。占比经参考海报校准：
    主视觉 0.56 / 卖点网格 0.20 / 细节带 0.18 / 底部条 0.06，归一化到画布高。
    """
    weights = {"hero": 0.56}
    if has_grid:
        weights["grid"] = 0.20
    if has_details:
        weights["details"] = 0.18
    if has_footer:
        weights["footer"] = 0.055
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

    # 字号按栏宽自适应：标题基准 0.135h，但若单行宽度超过 max_w 就按比例缩到能换行成
    # ≤2 行、且每行都不超宽。避免"标题太大直接冲出画布"（首版实测溢出）。
    def _fit_font(text: str, base_px: int, bold: bool, max_lines: int) -> Any:
        px = base_px
        for _ in range(24):
            f = load_font(max(12, int(px)), bold=bold)
            lines = C._wrap_text_balanced(text, f, max_w)
            if len(lines) <= max_lines and all(f.getlength(ln) <= max_w for ln in lines):
                return f
            px *= 0.94
        return load_font(max(12, int(px)), bold=bold)

    # 自底向上堆叠（贴主视觉区底部）：副标题 → 主标题 → 眉题
    y = y1 - pad

    if subheadline:
        font_s = _fit_font(subheadline, int(h * 0.050), False, 2)
        lines = C._wrap_text(subheadline, font_s, max_w)[:2]
        y -= len(lines) * int(font_s.size * 1.3)
        yy = y
        for ln in lines:
            draw.text((tx, yy), ln, font=font_s, fill=_muted(tc))
            yy += int(font_s.size * 1.3)

    if headline:
        font_h = _fit_font(headline, int(h * 0.130), True, 3)
        lines = C._wrap_text_balanced(headline, font_h, max_w)[:3]
        y -= len(lines) * int(font_h.size * 1.14)
        yy = y
        for ln in lines:
            draw.text((tx, yy), ln, font=font_h, fill=tc)
            yy += int(font_h.size * 1.14)

    if eyebrow:
        font_e = load_font(max(11, int(h * 0.034)), bold=True)
        y -= int(font_e.size * 2.0)
        draw.text((tx, y), eyebrow.upper(), font=font_e, fill=_muted(tc))
        # 眉题右侧的短横线（参考海报的分隔线）
        lw = int(font_e.getlength(eyebrow.upper()))
        line_x = tx + lw + int(font_e.size * 0.7)
        ly = y + int(font_e.size * 0.5)
        draw.rectangle([line_x, ly, min(x1 - pad, line_x + int(w * 0.14)), ly + max(2, int(h * 0.005))],
                       fill=_muted(tc))

    if brand:
        font_b = load_font(max(11, int(h * 0.032)), bold=True)
        # 品牌行在顶部：hero 顶部可能很亮，按背景亮度选字色，避免"白字压浅底"看不见
        b_y0 = y0 + int(h * 0.045)
        bl = C._region_luminance(poster, (int(tx), int(b_y0), int(tx + max_w), int(b_y0 + font_b.size)))
        b_col = (246, 246, 250) if bl < 140 else (28, 30, 36)
        draw.text((tx, b_y0), brand[:30], font=font_b, fill=b_col)


# ---------------------------------------------------------------- ② 卖点网格
def _draw_sellpoint_grid(
    poster: Image.Image,
    bullets: list[str],
    box: tuple[int, int, int, int],
    dark: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> None:
    """2×2 卖点网格：圆形描边图标 + 粗体小标题 + 一行说明。

    卖点文案约定为 `标题|说明`（缺省只有标题）。参考海报的 2×2 是这类
    电商海报最有辨识度的元素之一。
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    if not bullets:
        return
    draw = ImageDraw.Draw(poster)
    fg = _text_on(dark)
    pad_x = int(w * 0.05)
    pad_y = int(h * 0.06)
    cols, rows = 2, 2
    cw = (w - 2 * pad_x) // cols
    ch = (h - 2 * pad_y) // rows
    icon_r = int(min(cw, ch) * 0.15)
    font_t = load_font(max(11, int(ch * 0.24)), bold=True)
    font_d = load_font(max(9, int(ch * 0.16)))

    for i, b in enumerate(bullets[:4]):
        r, c = divmod(i, cols)
        cx = x0 + pad_x + c * cw
        cy = y0 + pad_y + r * ch
        icon_cx = cx + icon_r
        icon_cy = cy + icon_r
        # 圆形描边图标（放大镜/对勾等用统一圆环 + 简单记号，避免字体依赖）
        draw.ellipse(
            [icon_cx - icon_r, icon_cy - icon_r, icon_cx + icon_r, icon_cy + icon_r],
            outline=accent, width=max(2, int(icon_r * 0.22)),
        )
        # 圆内一个小对勾
        cw2 = max(2, int(icon_r * 0.20))
        pts = [(-0.38, 0.0), (-0.10, 0.28), (0.40, -0.30)]
        coords = [(icon_cx + int(px * icon_r), icon_cy + int(py * icon_r)) for px, py in pts]
        draw.line(coords, fill=accent, width=cw2, joint="curve")

        parts = [p.strip() for p in str(b).split("|", 1)]
        title = parts[0]
        desc = parts[1] if len(parts) > 1 else ""
        ty = icon_cy + icon_r + int(ch * 0.10)
        for ln in C._wrap_text(title, font_t, cw - icon_r)[:2]:
            draw.text((cx, ty), ln, font=font_t, fill=fg)
            ty += int(font_t.size * 1.18)
        if desc:
            ty += int(ch * 0.02)
            for ln in C._wrap_text(desc, font_d, cw - int(icon_r * 0.4))[:2]:
                draw.text((cx + int(icon_r * 0.4), ty), ln, font=font_d, fill=_muted(fg))
                ty += int(font_d.size * 1.2)


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
    cap_h = int(h * 0.22)
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
        # 细节图下方标题
        cap = captions[i] if i < len(captions) else ""
        if cap:
            font_c = load_font(max(10, int(cap_h * 0.42)), bold=True)
            ty = cy + img_h + int(cap_h * 0.18)
            for ln in C._wrap_text(cap, font_c, cell_w)[:2]:
                lw = int(font_c.getlength(ln))
                draw.text((cx + (cell_w - lw) // 2, ty), ln, font=font_c, fill=fg)
                ty += int(font_c.size * 1.2)


# ---------------------------------------------------------------- ④ 底部导航条
def _draw_scene_footer(
    poster: Image.Image,
    scenes: list[str],
    tagline: str,
    box: tuple[int, int, int, int],
    dark: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> None:
    """底部深色场景导航条：图标 + 场景词（Music/Study/Gaming/Work 式）。"""
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
    font_s = load_font(max(10, int(h * 0.30)))
    icon_r = int(h * 0.20)
    for i, sc in enumerate(scenes[:4]):
        cx = int(seg * (i + 0.5))
        # 小圆点图标
        draw.ellipse([cx - icon_r - int(seg * 0.16), y0 + h // 2 - icon_r,
                      cx - int(seg * 0.16) + icon_r, y0 + h // 2 + icon_r],
                     outline=accent, width=max(2, int(icon_r * 0.3)))
        draw.text((cx, y0 + (h - font_s.size) // 2), str(sc), font=font_s, fill=fg)


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
        caps = [str(b).split("|")[0] for b in (bullets or [])][:4]
        _draw_detail_strip(poster, detail_imgs, caps, dbox, dark)
        y += sec["details"]

    if has_footer:
        fbox = (0, y, W, H)
        _draw_scene_footer(poster, scenes or [], tagline, fbox, dark, accent)

    return poster.convert("RGB")
