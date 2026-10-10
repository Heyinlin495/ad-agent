"""平面广告合成：主体图 + 背景 + 文案渲染（Pillow 精确合成，保证主体保真）。"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from loguru import logger

from app.core.config import settings
from app.image.fonts import load_font, load_serif_font, reshape_rtl
from app.image.generator import make_gradient_background, resolve_size
from app.services.storage import storage


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _is_rtl(text: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in text)


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按宽度换行，兼容中文（逐字）与英文（逐词）。"""
    cjk = _has_cjk(text)
    lines: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        tokens: list[str] = list(para) if cjk else para.split(" ")
        current = ""
        for tok in tokens:
            candidate = current + tok if cjk else (current + " " + tok if current else tok)
            if not current or font.getlength(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current.strip())
                current = tok
        if current:
            lines.append(current.strip())
    return lines


def _wrap_text_balanced(
    text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """尽量等宽的换行，专门用来消除**孤字行**（末行只剩 1 个字）。

    贪心换行（`_wrap_text`）会"尽量往每行塞"，短文本就容易留下末行孤字，例如
    「沉浸式听觉盛宴」在窄栏被排成 `沉浸式 / 听觉盛 / 宴`。这里把 token 按行数**均分**，
    得到 `沉浸式听觉 / 盛宴` 这类更均衡的断法。

    只在贪心结果确实出现独字末行时才改写；否则原样返回，避免影响既有排版。
    英文按词边界重排、中文按字重排，保证不破坏语义单位。
    """
    greedy = _wrap_text(text, font, max_width)
    # 孤字的定义：末行仅剩 1 个字符（如 "沉浸式听觉盛宴" → ".../宴"）。
    # 2 个字符不算孤字（中文两字成词很常见，如 "视界"），阈值放宽可避免过度改写。
    if len(greedy) < 2 or len(greedy[-1].strip()) > 1:
        return greedy    # 没有孤字，保持原样

    cjk = _has_cjk(text)
    tokens: list[str] = [c for c in text if c.strip()] if cjk else text.split()
    if not tokens:
        return greedy

    def _join(chunk: list[str]) -> str:
        return "".join(chunk) if cjk else " ".join(chunk)

    n = len(greedy)

    def _try_balance(nlines: int) -> list[str] | None:
        """把 tokens 尽量均分成 nlines 行，每行都不得溢出栏宽；放不下则返回 None。

        均分会让各行长度最多差 1，末行因此天然不会只剩 1 个字（除非 tokens 本来就
        只比 nlines 多一个——那种情况下面会尝试增加行数）。
        """
        if nlines < 1 or nlines > len(tokens):
            return None
        base = len(tokens) // nlines
        extra = len(tokens) % nlines
        out: list[str] = []
        idx = 0
        for li in range(nlines):
            take = base + (1 if li < extra else 0)
            chunk = tokens[idx:idx + take]
            if not chunk:
                return None
            if font.getlength(_join(chunk)) > max_width:
                return None          # 该行放不下，此方案作废
            out.append(_join(chunk))
            idx += take
        return out

    # 行数可以保持不变，也可以多一两行——只要末行不再是孤字。
    for nlines in range(n, min(n + 3, len(tokens)) + 1):
        cand = _try_balance(nlines)
        if cand and len(cand) >= 2 and len(cand[-1].strip()) > 1:
            return cand
    return greedy


def _draw_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    max_width: int,
    color: tuple[int, int, int],
    *,
    align: str = "left",
    line_height: float = 1.3,
    stroke: int = 0,
    stroke_color: tuple[int, int, int] = (0, 0, 0),
    lines: list[str] | None = None,
) -> int:
    """绘制多行文本，返回结束后的 y 坐标。

    `lines` 可由调用方预传（度量阶段算好的换行结果），保证 **measure == draw**——
    否则均衡换行在度量阶段生效、绘制阶段又被贪心重排，行数与位置会对不上。
    """
    if _is_rtl(text):
        text = reshape_rtl(text)
        lines = None    # RTL 文本的 reshape 会改变字符，必须重新换行
    if lines is None:
        lines = _wrap_text(text, font, max_width)
    spacing = int(font.size * line_height)
    for line in lines:
        if align == "center":
            lw = int(font.getlength(line))
            lx = x + (max_width - lw) // 2
        elif align == "right":
            lw = int(font.getlength(line))
            lx = x + (max_width - lw)
        else:
            lx = x
        draw.text((lx, y), line, font=font, fill=color, stroke_width=stroke, stroke_fill=stroke_color)
        y += spacing
    return y


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: list[int],
    radius: int,
    fill: tuple[int, int, int] | None = None,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _blit_subject(
    canvas: Image.Image,
    subject: Image.Image,
    x: int,
    y: int,
    size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """按指定尺寸把主体粘贴到 (x, y)（带柔和投影），返回主体区域 (x, y, w, h)。"""
    subj = subject.resize(size, Image.LANCZOS)

    # 柔和投影（基于主体 alpha 轮廓）
    try:
        alpha = subj.split()[3]
        offset = int(subj.height * 0.04)
        blur_r = max(4, int(subj.height * 0.03))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        black = Image.new("RGBA", subj.size, (0, 0, 0, 200))
        shadow.paste(black, (x, y + offset), alpha)
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=blur_r))
        canvas.alpha_composite(shadow)
    except Exception:  # noqa: BLE001
        pass

    canvas.paste(subj, (x, y), subj)
    return x, y, subj.width, subj.height


def _paste_subject(
    canvas: Image.Image,
    subject: Image.Image,
    box_ratio: float = 0.6,
    center: tuple[float, float] = (0.5, 0.40),
) -> tuple[int, int, int, int]:
    """缩放并粘贴主体图（带柔和投影），返回主体区域 (x, y, w, h)。"""
    max_dim = min(canvas.size) * box_ratio
    scale = max_dim / max(subject.size)
    new_size = (max(1, int(subject.width * scale)), max(1, int(subject.height * scale)))
    x = int(canvas.width * center[0] - new_size[0] / 2)
    y = int(canvas.height * center[1] - new_size[1] / 2)
    return _blit_subject(canvas, subject, x, y, new_size)


def _fit_size(
    subject_size: tuple[int, int],
    rect: tuple[int, int, int, int],
    max_foot: float,
) -> tuple[int, int]:
    """把主体等比缩放进 rect（画布坐标），并限制最长边不超过 max_foot。"""
    rw = max(1, rect[2] - rect[0])
    rh = max(1, rect[3] - rect[1])
    sw, sh = subject_size
    if sw <= 0 or sh <= 0:
        return (1, 1)
    ar = sw / sh
    if rw / ar <= rh:
        tw, th = rw, max(1, int(rw / ar))
    else:
        tw, th = max(1, int(rh * ar)), rh
    if max(tw, th) > max_foot:
        k = max_foot / max(tw, th)
        tw, th = max(1, int(tw * k)), max(1, int(th * k))
    return tw, th


def _rects_hit(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int] | None,
) -> bool:
    """两个矩形是否相交（含边重叠）。两个参数都必须是 (x0, y0, x1, y1)。

    b 为 None 表示"位置未知"，一律返回 False（不判为冲突），由调用方决定是否绘制。
    """
    if b is None:
        return False
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _xywh_to_rect(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """(x, y, w, h) -> (x0, y0, x1, y1)，供相交判定统一坐标约定。"""
    x, y, w, h = box
    return (x, y, x + w, y + h)


def _region_luminance(canvas: Image.Image, box: tuple[int, int, int, int]) -> float:
    """采样指定区域的平均亮度（0-255）。"""
    crop = canvas.convert("RGB").crop(box)
    crop = crop.resize((1, 1))
    r, g, b = crop.getpixel((0, 0))
    return 0.299 * r + 0.587 * g + 0.114 * b


def _contrast_ratio(lum_a: float, lum_b: float) -> float:
    """WCAG 对比度比值（1~21）。文字可读性经验门槛：≥4.5 良好，≥3.0 最低可接受。"""
    def _lin(v: float) -> float:
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    la, lb = _lin(lum_a), _lin(lum_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _text_color_for(canvas: Image.Image, region: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """按背景亮度自动选择文字颜色（深底浅字 / 浅底深字）。

    用**区域平均亮度**选深/浅色即可，但衬底是否必要由 `_needs_scrim` 单独判定——
    均匀浅底配深字本就清晰，不需要再加衬底（加多了很脏）。
    """
    lum = _region_luminance(canvas, region)
    return (245, 245, 245) if lum < 128 else (30, 30, 30)


def _needs_scrim(canvas: Image.Image, region: tuple[int, int, int, int],
                 text_color: tuple[int, int, int]) -> int:
    """判断是否需要给文字加衬底以保证对比度。

    旧实现只在"文字与主体相交"时加衬底，导致**白字压在浅色木纹/高光渐变上**
    这类"不相交但对比极低"的情况完全不管——文案肉眼几乎不可见（线上任务 #3）。
    改为按 WCAG 对比度判定：区域内**比对色低的那一端**（取区域亮度的 p10/p90 近似）
    仍达不到 3.0 时，才需要衬底。返回 0=不需要，1=需要。
    """
    crop = canvas.convert("RGB").crop(region)
    if crop.width < 2 or crop.height < 2:
        return 0
    small = crop.resize((max(2, min(32, crop.width)), max(2, min(32, crop.height))))
    lum_t = 0.299 * text_color[0] + 0.587 * text_color[1] + 0.114 * text_color[2]
    worst = 21.0
    for r, g, b in small.getdata():
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        worst = min(worst, _contrast_ratio(lum, lum_t))
    return 1 if worst < 3.0 else 0


def _pick_accent(design: dict[str, Any], text_color: tuple[int, int, int]) -> tuple[int, int, int] | None:
    """从视觉方案 palette 中挑选强调色（品牌色）：取饱和度最高、且与文字色可区分的颜色。

    无合适候选（palette 缺失/全灰/与文字同色）时返回 None，由调用方回退。
    """
    best: tuple[int, int, int] | None = None
    best_score = 0.0
    tc_lum = 0.299 * text_color[0] + 0.587 * text_color[1] + 0.114 * text_color[2]
    for c in design.get("palette") or []:
        try:
            rgb = _hex_to_rgb(str(c))
        except (ValueError, TypeError):
            continue
        sat = float(max(rgb) - min(rgb))
        lum = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
        if abs(lum - tc_lum) < 45:  # 与文字亮度太接近，做强调色会看不清
            continue
        if sat > best_score:
            best, best_score = rgb, sat
    return best


def _band_color_for(canvas: Image.Image, region: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """文字衬底颜色：与背景亮度相反，半透明。

    浅背景用**深色**衬底（保证深色字或反白字都够对比），深背景用**浅色**衬底。
    透明度略提高（180/200）以避免"看着有衬底却仍读不清"。
    """
    lum = _region_luminance(canvas, region)
    return (255, 255, 255, 205) if lum < 128 else (18, 18, 20, 180)


def _draw_text_band(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int, int],
) -> None:
    """在独立图层上画半透明圆角衬底，再合成到画布。"""
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(list(box), radius=radius, fill=fill)
    canvas.alpha_composite(overlay)


# ---------- 版式模板 ----------


def _tpl_centered(canvas, subject, design, texts, text_color):
    w, h = canvas.size
    _paste_subject(canvas, subject, box_ratio=design.get("_subject_scale", 0.62), center=(0.5, 0.40))

    # 底部文字衬底
    band_top = int(h * 0.70)
    _draw_text_band(canvas, (int(w * 0.06), band_top, int(w * 0.94), h - int(h * 0.03)), int(h * 0.03), _band_color_for(canvas, (0, band_top, w, h)))

    headline_region = (int(w * 0.08), band_top, int(w * 0.92), h)
    tc = _text_color_for(canvas, headline_region)
    font_h = load_font(int(h * 0.052), bold=True, rtl=_is_rtl(texts["headline"]))
    font_s = load_font(int(h * 0.028))
    draw = ImageDraw.Draw(canvas)
    y = int(h * 0.725)
    y = _draw_text_block(draw, texts["headline"], font_h, int(w * 0.08), y, int(w * 0.84), tc, align="center", stroke=1, stroke_color=tc)
    if texts["subheadline"]:
        y = _draw_text_block(draw, texts["subheadline"], font_s, int(w * 0.12), y + int(h * 0.008), int(w * 0.76), tc, align="center")
    if texts["cta"]:
        _draw_cta_button(canvas, texts["cta"], (int(w * 0.32), y + int(h * 0.02), int(w * 0.68), y + int(h * 0.02) + int(h * 0.075)))


def _tpl_left_text(canvas, subject, design, texts, text_color):
    w, h = canvas.size
    _paste_subject(canvas, subject, box_ratio=design.get("_subject_scale", 0.62), center=(0.70, 0.48))

    # 左侧文字衬底
    _draw_text_band(canvas, (int(w * 0.04), int(h * 0.18), int(w * 0.46), int(h * 0.86)), int(h * 0.02), _band_color_for(canvas, (0, int(h * 0.18), int(w * 0.46), int(h * 0.86))))

    region = (int(w * 0.05), int(h * 0.18), int(w * 0.45), int(h * 0.86))
    tc = _text_color_for(canvas, region)
    draw = ImageDraw.Draw(canvas)
    font_h = load_font(int(h * 0.055), bold=True, rtl=_is_rtl(texts["headline"]))
    font_s = load_font(int(h * 0.03))
    x = int(w * 0.07)
    y = int(h * 0.24)
    y = _draw_text_block(draw, texts["headline"], font_h, x, y, int(w * 0.38), tc, stroke=1, stroke_color=tc)
    if texts["subheadline"]:
        y = _draw_text_block(draw, texts["subheadline"], font_s, x, y + int(h * 0.02), int(w * 0.38), tc)
    if texts["cta"]:
        _draw_cta_button(canvas, texts["cta"], (x, y + int(h * 0.04), x + int(w * 0.30), y + int(h * 0.04) + int(h * 0.075)))


def _tpl_promo_badge(canvas, subject, design, texts, text_color):
    w, h = canvas.size
    _paste_subject(canvas, subject, box_ratio=design.get("_subject_scale", 0.58), center=(0.5, 0.42))
    # 促销角标
    r = int(h * 0.12)
    cx, cy = int(w * 0.84), int(h * 0.14)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(230, 57, 70))
    badge = texts.get("promo") or "SALE"
    fb = load_font(int(r * 0.62), bold=True)
    btw = int(fb.getlength(badge))
    draw.text((cx - btw // 2, cy - int(fb.size * 0.55)), badge, font=fb, fill=(255, 255, 255))

    band_top = int(h * 0.70)
    _draw_text_band(canvas, (int(w * 0.06), band_top, int(w * 0.94), h - int(h * 0.03)), int(h * 0.03), _band_color_for(canvas, (0, band_top, w, h)))
    tc = _text_color_for(canvas, (int(w * 0.08), band_top, int(w * 0.92), h))
    font_h = load_font(int(h * 0.05), bold=True, rtl=_is_rtl(texts["headline"]))
    y = int(h * 0.725)
    _draw_text_block(draw, texts["headline"], font_h, int(w * 0.08), y, int(w * 0.84), tc, align="center", stroke=1, stroke_color=tc)


def _tpl_compare(canvas, subject, design, texts, text_color):
    w, h = canvas.size
    _paste_subject(canvas, subject, box_ratio=design.get("_subject_scale", 0.58), center=(0.26, 0.45))

    # 右侧卖点衬底
    _draw_text_band(canvas, (int(w * 0.50), int(h * 0.14), int(w * 0.95), int(h * 0.88)), int(h * 0.02), _band_color_for(canvas, (int(w * 0.50), int(h * 0.14), int(w * 0.95), int(h * 0.88))))

    tc = _text_color_for(canvas, (int(w * 0.52), int(h * 0.14), int(w * 0.93), int(h * 0.88)))
    draw = ImageDraw.Draw(canvas)
    x = int(w * 0.54)
    y = int(h * 0.18)
    font_h = load_font(int(h * 0.048), bold=True, rtl=_is_rtl(texts["headline"]))
    y = _draw_text_block(draw, texts["headline"], font_h, x, y, int(w * 0.40), tc, stroke=1, stroke_color=tc)
    font_b = load_font(int(h * 0.03))
    y += int(h * 0.03)
    for bullet in texts.get("bullets", [])[:4]:
        marker = "• " if not _is_rtl(bullet) else ""
        y = _draw_text_block(draw, marker + bullet, font_b, x, y, int(w * 0.40), tc)
        y += int(h * 0.015)


def _draw_chip(
    canvas: Image.Image,
    item: dict[str, Any],
    y: int,
    fg: tuple[int, int, int],
    outline: tuple[int, int, int] | None = None,
) -> None:
    """绘制单个卖点徽章（半透明圆角衬底 + 描边 + 文字）；几何来自排版数据。"""
    stroke = outline if outline is not None else fg
    x = int(item["x"])
    box = [x, y, x + int(item["box_w"]), y + int(item["box_h"])]
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    fill = (255, 255, 255, 40) if sum(fg) > 380 else (0, 0, 0, 60)
    od.rounded_rectangle(box, radius=int(item["box_h"]) // 2, fill=fill, outline=stroke + (220,), width=2)
    canvas.alpha_composite(overlay)
    ImageDraw.Draw(canvas).text(
        (x + int(item["pad_x"]), y + int(item["pad_y"])),
        str(item["text"]),
        font=item["font"],
        fill=fg,
    )


def _draw_feature_row(
    canvas: Image.Image,
    items: list[str],
    fg: tuple[int, int, int],
    accent: tuple[int, int, int] | None = None,
    icon: str = "check",
) -> None:
    """底部图标卖点行：圆形描边图标 + 下方标签，均分整行。

    图标形状按品类适配：'bars' 为声波柱状（音频设备），'check' 为通用对勾。
    圆圈描边用强调色（品牌色），无强调色时回退文字色。
    """
    w, h = canvas.size
    n = len(items)
    if n == 0:
        return
    draw = ImageDraw.Draw(canvas)
    ring = accent if accent is not None else fg
    seg = w / n
    icon_r = int(h * 0.026)
    icon_cy = int(h * 0.875)
    font_l = load_font(int(h * 0.020))
    for i, it in enumerate(items):
        cx = int(seg * (i + 0.5))
        # 圆形描边
        draw.ellipse(
            [cx - icon_r, icon_cy - icon_r, cx + icon_r, icon_cy + icon_r],
            outline=ring, width=2,
        )
        if icon == "bars":
            # 声波柱状小图标（3 根高低错落的竖条，仅音频类产品）
            bw = max(2, int(icon_r * 0.16))
            heights = [0.55, 0.95, 0.55]
            for k, hr in enumerate(heights):
                bx = cx + int((k - 1) * icon_r * 0.5) - bw // 2
                bh = int(icon_r * hr)
                draw.rectangle([bx, icon_cy - bh // 2, bx + bw, icon_cy + bh // 2], fill=fg)
        else:
            # 通用对勾图标
            cw = max(2, int(icon_r * 0.18))
            pts = [(-0.42, 0.02), (-0.12, 0.32), (0.45, -0.32)]
            coords = [(cx + int(px * icon_r), icon_cy + int(py * icon_r)) for px, py in pts]
            draw.line(coords, fill=fg, width=cw, joint="curve")
        # 标签（居中，最多两行）
        lines = _wrap_text(it, font_l, int(seg * 0.9))[:2]
        ly = icon_cy + icon_r + int(h * 0.010)
        for line in lines:
            lw = int(font_l.getlength(line))
            draw.text((cx - lw // 2, ly), line, font=font_l, fill=fg)
            ly += int(font_l.size * 1.25)


def _hero_text_layout(
    canvas: Image.Image,
    design: dict[str, Any],
    texts: dict[str, Any],
    x: int,
    max_w: int,
    scale: float = 1.0,
) -> tuple[int, list[dict[str, Any]], int]:
    """计算 hero 文字块排版（不含颜色），返回 (块高度, items, 实际内容宽度)。

    items 的 y 为「相对块顶点」的坐标，落笔时统一加上起始 y。度量与绘制共用同一份
    排版数据，因此行数、换行位置完全一致——这是"文字不压主体"能成立的前提：
    先量出文字实际占多大，再据此给主体让位。
    第三项为文字真正落笔的最大右边界（左对齐下通常远小于 max_w），
    供品类标签/图标行做精确避让，避免"留白被误判为占用"。

    `scale` 统一缩放所有字号与间距（默认 1.0）。**注意反直觉之处**：把文字排进
    更窄的栏会让它换行更多、块变得**更高**（实测 828 图：栏宽 736 → 块高 42% 画高，
    栏宽 165 → 块高 85% 画高）。所以"避开居中主体"不能只靠挪位置/收窄栏宽，
    必要时必须整体缩小字号，否则块高必然横跨主体。
    """
    w, h = canvas.size
    items: list[dict[str, Any]] = []
    y = 0
    right = x  # 内容实际右边界
    s = max(0.45, float(scale))

    # ⚠️ 以下基准字号是**画布固定比例**，被两条链路共用：
    #   ① compose_ad_image → _tpl_hero（抠图贴图，文字与主体争夺画布）
    #      这里的块高直接决定留给主体的自由区大小，改大会让宽扁产品（电视/显示器）
    #      被挤成窄条，test_hero_wide_product_uses_full_width_band 会失败。
    #   ② compose_ad_from_image → _tpl_hero_overlay / _tpl_magazine_overlay（整图叠加）
    #      主体已在画面里，文字只需避开，故通过外层 `scale` 系数放大即可（不动基准值）。
    # 因此**放大字号只能走 ② 的 scale 系数**，不要直接上调这里的比例——会连带改变 ①
    # 的主体占位。历史事故：把标题从 0.075 上调到 0.088，① 的宽扁产品主体宽度
    # 从 648px 掉到 579px，触发回归失败。
    brand = (design.get("_brand") or "").strip()
    if brand:
        fb = load_font(max(9, int(h * 0.026 * s)), bold=True)
        shown = brand[:24]
        while shown and fb.getlength(shown) > max_w:  # 品牌行不换行，超宽则截断
            shown = shown[:-1]
        items.append({"kind": "text", "text": shown, "font": fb, "x": x, "y": y,
                      "max_w": max_w, "align": "left", "stroke": 0, "paint": "accent"})
        right = max(right, x + int(fb.getlength(shown)))
        y += int(fb.size * 1.7)

    font_h = load_font(max(14, int(h * 0.075 * s)), bold=True, rtl=_is_rtl(texts["headline"]))
    if texts["headline"]:
        # 标题用均衡换行：窄栏 + 大字号下贪心换行极易留下孤字末行（"…/宴"）。
        # 均衡换行能在**不改字号**的前提下消除孤字，比"为了躲孤字而降字号"更划算。
        lines = _wrap_text_balanced(texts["headline"], font_h, max_w)
        items.append({"kind": "text", "text": texts["headline"], "font": font_h, "x": x,
                      "y": y, "max_w": max_w, "align": "left", "stroke": 1, "paint": "tc",
                      "lines": lines})
        right = max(right, x + max((int(font_h.getlength(ln)) for ln in lines), default=0))
        y += len(lines) * int(font_h.size * 1.3)

    font_s = load_font(max(10, int(h * 0.028 * s)))
    if texts["subheadline"]:
        lines = _wrap_text(texts["subheadline"], font_s, max_w)
        y += int(h * 0.012 * s)
        items.append({"kind": "text", "text": texts["subheadline"], "font": font_s, "x": x,
                      "y": y, "max_w": max_w, "align": "left", "stroke": 0, "paint": "tc"})
        right = max(right, x + max((int(font_s.getlength(ln)) for ln in lines), default=0))
        y += len(lines) * int(font_s.size * 1.3)
        bar_h = max(3, int(h * 0.005 * s))
        rule_w = min(int(w * 0.075), max_w)
        items.append({"kind": "rule", "x": x, "y": y + int(h * 0.010 * s), "w": rule_w, "h": bar_h})
        right = max(right, x + rule_w)
        y += int(h * 0.010 * s) + bar_h

    bullets = [b for b in texts.get("bullets", []) if b and b.strip()]
    if bullets:
        # 卖点字号 2.4% 画高偏小，整图叠加链路会靠外层 scale 放大到可读；
        # 过长的卖点截断到合理长度，避免放大后单个 chip 撑满整栏宽度。
        font_chip = load_font(max(9, int(h * 0.024 * s)), bold=True)
        pad_x = int(font_chip.size * 0.9)
        pad_y = int(font_chip.size * 0.5)
        gap = int(font_chip.size * 0.55)
        cy = y + int(h * 0.018 * s)
        last_bottom = cy
        for it in bullets[:3]:
            label = _shorten_chip(it, font_chip, max_w)
            tw = int(font_chip.getlength(label))
            box_w = tw + pad_x * 2
            box_h = int(font_chip.size) + pad_y * 2
            items.append({"kind": "chip", "text": label, "font": font_chip, "x": x, "y": cy,
                          "box_w": box_w, "box_h": box_h, "pad_x": pad_x, "pad_y": pad_y})
            right = max(right, x + box_w)
            last_bottom = cy + box_h
            cy = last_bottom + gap
        y = last_bottom

    if texts["cta"]:
        cta_h = int(h * 0.065 * s)
        # CTA 宽度必须夹到栏宽以内：固定比例 0.26w 在窄栏（侧栏约 0.16w）下会溢出栏宽，
        # 让内容框（cw）算得比栏还宽 → 相交判定失效、文字越界（实测 cw=936 > 栏宽 320）。
        cta_w = min(int(w * 0.26 * s), max_w)
        cy0 = y + int(h * 0.02 * s)
        items.append({"kind": "cta", "text": texts["cta"], "x": x, "y": cy0, "w": cta_w, "h": cta_h})
        right = max(right, x + cta_w)
        y = cy0 + cta_h

    # 内容宽度不得超过栏宽（单个超长英文单词/品牌行可能溢出），否则相交判定会失效
    return y, items, min(right - x, max_w)


def _shorten_chip(text: str, font: Any, max_w: int) -> str:
    """把过长的卖点截断到单行可容纳（chip 不换行）。

    字号放大后原来能放下的整句会超栏宽，若不截断会溢出到主体上。
    优先在词边界断开并加省略号；单片 chip 不超过栏宽的 92%。
    """
    limit = int(max_w * 0.92)
    t = str(text).strip()
    if not t or font is None:
        return t
    if font.getlength(t) <= limit:
        return t
    words = t.split()
    out = ""
    for wd in words:
        cand = f"{out} {wd}".strip()
        if font.getlength(cand + "…") > limit:
            break
        out = cand
    if not out:  # 单个超长词：硬切
        out = t
        while out and font.getlength(out + "…") > limit:
            out = out[:-1]
    return out + "…"


def _hero_draw(
    canvas: Image.Image,
    items: list[dict[str, Any]],
    y0: int,
    tc: tuple[int, int, int],
    accent: tuple[int, int, int] | None,
) -> None:
    """按排版数据绘制 hero 文字块（含颜色解析）。"""
    draw = ImageDraw.Draw(canvas)
    for it in items:
        y = int(it["y"]) + y0
        kind = it["kind"]
        if kind == "text":
            color = accent if (it["paint"] == "accent" and accent) else tc
            _draw_text_block(draw, it["text"], it["font"], int(it["x"]), y, int(it["max_w"]),
                             color, align=it["align"], stroke=it["stroke"], stroke_color=color,
                             lines=it.get("lines"))
        elif kind == "rule":
            draw.rectangle([int(it["x"]), y, int(it["x"]) + int(it["w"]), y + int(it["h"])],
                           fill=accent if accent else tc)
        elif kind == "chip":
            _draw_chip(canvas, it, y, tc, outline=accent)
        elif kind == "cta":
            _draw_cta_button(canvas, it["text"],
                             (int(it["x"]), y, int(it["x"]) + int(it["w"]), y + int(it["h"])),
                             fill=accent)


def _tpl_hero(canvas, subject, design, texts, text_color):
    """高级感大特写海报：品牌行 + 大标题 + 卖点徽章 + 底部场景图标行 + 品类标签。

    关键约束：**文字绝不覆盖产品主体**。
    做法是先量出文字块的实际尺寸，再在「顶部通栏 / 左侧栏 / 右侧栏 / 底部通栏」四个
    候选版位中，挑出让主体可用面积最大的那个（宽扁产品如电视会自然落到上下通栏，
    高瘦产品如音箱会落到左右栏），然后把主体等比放进剩余空白区居中。
    强调色取自视觉方案 palette（按产品自适应），用于强调线、徽章描边、CTA 与图标圈。
    """
    w, h = canvas.size
    mx = int(w * 0.06)
    my = int(h * 0.05)
    gutter = max(14, int(min(w, h) * 0.045))
    try:
        max_scale = float(design.get("_subject_scale") or 0.72)
    except (TypeError, ValueError):
        max_scale = 0.72
    max_foot = min(w, h) * max_scale

    col_w = int(w * 0.46)
    wide_w = w - 2 * mx
    # 左右栏版位为底部场景图标行预留一条横带，避免图标行与主体/文字打架
    row_strip = int(h * 0.12)

    candidates: list[dict[str, Any]] = []
    # 顶部通栏 / 左侧栏 / 右侧栏（文字占据左上或右上，块高由内容决定）
    #
    # 为什么这里用「固定比例字号」而不是像 hero_overlay 那样二分放大：
    # 本版式里**文字与主体争夺同一块画布**——文字块越高，留给主体的自由区就越小。
    # 若按"塞满可用高度"最大化字号，文字块会膨胀到 ~90% 画高（实测），主体被挤成
    # 一条窄缝，宽扁产品（电视/显示器）直接退化成右侧小图（回归测试会失败）。
    # 所以这里保持字号为画布固定比例（见 _hero_text_layout），靠下方候选评分在
    # "上下通栏 / 左右栏"之间按主体可用面积择优。放大字号的需求由整图叠加链路
    # （_tpl_hero_overlay）通过 scale 系数单独满足，本版式不参与。
    for zone, tx, tw in (
        ("top", mx, wide_w),
        ("left", mx, col_w),
        ("right", w - mx - col_w, col_w),
    ):
        th, items, cw = _hero_text_layout(canvas, design, texts, tx, tw)
        candidates.append({"zone": zone, "x": tx, "y": my, "w": tw, "h": th,
                           "cw": cw, "items": items})
    # 底部通栏（需要先量高度，再贴到下沿）
    th_b, items_b, cw_b = _hero_text_layout(canvas, design, texts, mx, wide_w)
    candidates.append({"zone": "bottom", "x": mx, "y": h - my - th_b, "w": wide_w,
                       "h": th_b, "cw": cw_b, "items": items_b})

    best: dict[str, Any] | None = None
    for cand in candidates:
        tx0, ty0 = cand["x"], cand["y"]
        tx1, ty1 = tx0 + cand["w"], ty0 + cand["h"]
        if cand["zone"] == "top":
            free = (mx, ty1 + gutter, w - mx, h - my)
        elif cand["zone"] == "bottom":
            free = (mx, my, w - mx, ty0 - gutter)
        elif cand["zone"] == "left":
            free = (tx1 + gutter, my, w - mx, h - my - row_strip)
        else:  # right
            free = (mx, my, tx0 - gutter, h - my - row_strip)
        if free[2] - free[0] < 60 or free[3] - free[1] < 60:
            continue
        size = _fit_size(subject.size, free, max_foot)
        cand["free"] = free
        cand["size"] = size
        cand["area"] = size[0] * size[1]
        if best is None or cand["area"] > best["area"]:
            best = cand

    if best is None:  # 极端兜底：左栏 + 最小可用区域
        th, items, cw = _hero_text_layout(canvas, design, texts, mx, col_w)
        free = (mx + col_w + gutter, my, w - mx, h - my - row_strip)
        best = {"zone": "left", "x": mx, "y": my, "w": col_w, "h": th, "cw": cw, "items": items,
                "free": free, "size": _fit_size(subject.size, free, max_foot)}
        best["area"] = best["size"][0] * best["size"][1]

    # 主体居中放入自由区；记录主体框供版式调试与回归比对
    fx0, fy0, fx1, fy1 = best["free"]
    tw_, th_ = best["size"]
    sx = int((fx0 + fx1) / 2 - tw_ / 2)
    sy = int((fy0 + fy1) / 2 - th_ / 2)
    design["_subject_box"] = list(_blit_subject(canvas, subject, sx, sy, (tw_, th_)))
    subject_rect = _xywh_to_rect(tuple(design["_subject_box"]))

    # 文字区底色自适应（此时背景与主体均已就位，且文字区与主体保证不相交）
    tx0, ty0 = best["x"], best["y"]
    tx1, ty1 = tx0 + best["w"], ty0 + best["h"]
    pad = max(6, int(min(w, h) * 0.01))
    tc = _text_color_for(canvas, (max(0, tx0 - pad), max(0, ty0 - pad),
                                  min(w, tx1 + pad), min(h, ty1 + pad)))
    accent = _pick_accent(design, tc)

    _hero_draw(canvas, best["items"], ty0, tc, accent)
    # 文字实际占位：左对齐，右边界取真实落笔宽度（而非预留列宽），
    # 供装饰元素做精确避让
    text_right = min(tx1, tx0 + int(best.get("cw") or best["w"]))
    text_rect = (tx0, ty0, text_right, ty1)
    design["_text_box"] = list(text_rect)

    # 右上角品类标签（如 AUDIO / BEAUTY）：与主体、文字都不冲突时才绘制
    tag = (design.get("_tag") or "").strip()
    if tag:
        font_tag = load_font(int(h * 0.020), bold=True)
        ttw = int(font_tag.getlength(tag))
        tag_box = (w - mx - ttw, int(h * 0.045), w - mx, int(h * 0.045) + int(font_tag.size * 1.3))
        if not _rects_hit(tag_box, subject_rect) and not _rects_hit(tag_box, text_rect):
            ImageDraw.Draw(canvas).text((tag_box[0], tag_box[1]), tag, font=font_tag, fill=tc)
            design["_tag_box"] = list(tag_box)

    # 底部场景图标行：优先用产品使用场景（scenes），无场景回退卖点，避免文案重复。
    # 仅在既不压主体、也不压文字时绘制（左右栏版位已预留横带，通常可正常绘制）。
    bullets = [b for b in texts.get("bullets", []) if b and b.strip()]
    row_items = design.get("_scenes") or bullets[:4]
    if row_items:
        row_box = (0, int(h * 0.83), w, int(h * 0.98))
        if not _rects_hit(row_box, subject_rect) and not _rects_hit(row_box, text_rect):
            _draw_feature_row(canvas, row_items[:4], tc, accent=accent, icon=design.get("_icon", "check"))


# ---------- magazine 杂志版式（左侧文字 / 右侧主体，衬线体大标题）----------
# 对照"商业大模型"级别的时尚广告版式：眉题(brand line) + 衬线大标题 + 副标 +
# 细分割线 + 带圆形图标的卖点列表 + 深色 CTA 按钮；文字整体落在画面一侧的留白区，
# 主体占据另一侧，二者物理分离。与 hero 一样遵守「文字不压主体」硬约束。

def _magazine_text_layout(
    canvas: Image.Image,
    design: dict[str, Any],
    texts: dict[str, Any],
    x: int,
    max_w: int,
) -> tuple[int, list[dict[str, Any]], int]:
    """计算 magazine 文字块排版（度量与绘制共用，保证 measure == draw）。

    返回 (块高度, items, 实际内容宽度)。items 的 y 为相对块顶点的坐标。
    """
    w, h = canvas.size
    items: list[dict[str, Any]] = []
    y = 0
    right = x

    # 眉题（品牌行 / 栏目名），字距拉开、全大写，弱化处理
    brand = (design.get("_brand") or "").strip()
    if brand:
        fb = load_font(int(h * 0.020))
        shown = brand[:30]
        while shown and fb.getlength(shown) > max_w:
            shown = shown[:-1]
        items.append({"kind": "text", "text": shown, "font": fb, "x": x, "y": y,
                      "max_w": max_w, "align": "left", "stroke": 0,
                      "paint": "muted", "tracking": True})
        right = max(right, x + int(fb.getlength(shown)))
        y += int(fb.size * 2.0)

    # 衬线大标题（杂志核心视觉）。字号自适应列宽：列窄时自动降字号，
    # 保证最长单词不溢出列（否则衬线标题会横向压到主体上）。
    head_size = int(h * 0.078)
    min_head = int(h * 0.040)
    if texts["headline"]:
        longest = max(texts["headline"].split(), key=len, default="")
        while head_size > min_head:
            f = load_serif_font(head_size)
            if f.getlength(longest) <= max_w:
                break
            head_size -= 2
    font_h = load_serif_font(head_size)
    if texts["headline"]:
        lines = _wrap_text(texts["headline"], font_h, max_w)
        items.append({"kind": "text", "text": texts["headline"], "font": font_h, "x": x,
                      "y": y, "max_w": max_w, "align": "left", "stroke": 0, "paint": "tc",
                      "serif": True, "line_height": 1.12})
        right = max(right, x + max((int(font_h.getlength(ln)) for ln in lines), default=0))
        y += int(len(lines) * font_h.size * 1.12)

    # 副标
    font_s = load_font(int(h * 0.021))
    if texts["subheadline"]:
        y += int(h * 0.014)
        lines = _wrap_text(texts["subheadline"], font_s, max_w)
        items.append({"kind": "text", "text": texts["subheadline"], "font": font_s, "x": x,
                      "y": y, "max_w": max_w, "align": "left", "stroke": 0,
                      "paint": "muted", "line_height": 1.45})
        right = max(right, x + max((int(font_s.getlength(ln)) for ln in lines), default=0))
        y += int(len(lines) * font_s.size * 1.45)

    # 细分割线（杂志感的关键细节）
    rule_w = int(max_w * 0.16)
    bar_h = max(2, int(h * 0.002))
    y += int(h * 0.026)
    items.append({"kind": "rule", "x": x, "y": y, "w": rule_w, "h": bar_h})
    right = max(right, x + rule_w)

    # 卖点：圆形描边图标 + 文字（横向一行一个），比 hero 的 chips 更克制高级
    bullets = [b for b in texts.get("bullets", []) if b and b.strip()][:3]
    if bullets:
        font_b = load_font(int(h * 0.019))
        icon_r = int(font_b.size * 0.62)
        gap_x = int(icon_r * 1.5)
        cy = y + int(h * 0.030)
        text_x = x + icon_r * 2 + gap_x
        text_w = max_w - (icon_r * 2 + gap_x)
        for it in bullets:
            lines = _wrap_text(it, font_b, text_w)[:2]
            text_h = int(len(lines) * font_b.size * 1.3)
            block_h = max(icon_r * 2, text_h)
            items.append({"kind": "iconbullet", "text": it, "font": font_b,
                          "x": x, "y": cy, "icon_r": icon_r, "gap_x": gap_x,
                          "text_x": text_x, "text_w": text_w, "lines": lines,
                          "block_h": block_h})
            right = max(right, min(text_x + max((int(font_b.getlength(ln)) for ln in lines), default=0),
                                   x + max_w))
            cy += block_h + int(h * 0.016)
        y = cy - int(h * 0.016)  # 回退到最后一个 block 的底边

    # CTA：深色实心按钮（杂志风）
    if texts["cta"]:
        cta_h = int(h * 0.052)
        font_c = load_font(int(cta_h * 0.40), bold=True)
        cta_text = texts["cta"][:26]
        cta_w = int(font_c.getlength(cta_text)) + int(cta_h * 1.7)
        cta_h_pad = int(cta_h * 0.55)
        cy0 = y + int(h * 0.042)  # 与上方内容拉开足够间距，避免压住最后一条卖点
        items.append({"kind": "solidcta", "text": cta_text, "font": font_c,
                      "x": x, "y": cy0, "w": cta_w, "h": cta_h_pad, "h_pad": cta_h_pad})
        right = max(right, x + cta_w)
        y = cy0 + cta_h_pad

    return y, items, right - x


def _magazine_draw(
    canvas: Image.Image,
    items: list[dict[str, Any]],
    y0: int,
    tc: tuple[int, int, int],
    muted: tuple[int, int, int],
    accent: tuple[int, int, int] | None,
) -> None:
    """按排版数据绘制 magazine 文字块。"""
    draw = ImageDraw.Draw(canvas)
    for it in items:
        y = int(it["y"]) + y0
        kind = it["kind"]
        if kind == "text":
            color = accent if (it["paint"] == "accent" and accent) else (
                muted if it["paint"] == "muted" else tc)
            if it.get("tracking"):
                # 字距拉开（眉题）：逐字绘制并累计 advance
                cx = it["x"]
                for ch in it["text"]:
                    draw.text((cx, y), ch, font=it["font"], fill=color)
                    cx += int(it["font"].getlength(ch)) + max(1, int(it["font"].size * 0.18))
            else:
                lh = float(it.get("line_height") or 1.3)
                _draw_text_block(draw, it["text"], it["font"], int(it["x"]), y,
                                 int(it["max_w"]), color, align=it["align"],
                                 stroke=it["stroke"], stroke_color=color, line_height=lh)
        elif kind == "rule":
            draw.rectangle([int(it["x"]), y, int(it["x"]) + int(it["w"]), y + int(it["h"])],
                           fill=accent if accent else tc)
        elif kind == "iconbullet":
            r = int(it["icon_r"])
            cx = int(it["x"]) + r
            cy = y + r
            ring = accent if accent else tc
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ring, width=2)
            # 圈内对勾
            cw = max(2, int(r * 0.24))
            pts = [(-0.36, 0.02), (-0.10, 0.28), (0.40, -0.30)]
            draw.line([(cx + int(px * r), cy + int(py * r)) for px, py in pts],
                      fill=ring, width=cw, joint="curve")
            ly = y + max(0, (r * 2 - int(len(it["lines"]) * it["font"].size * 1.3)) // 2)
            for ln in it["lines"]:
                draw.text((int(it["text_x"]), ly), ln, font=it["font"], fill=tc)
                ly += int(it["font"].size * 1.3)
        elif kind == "solidcta":
            h_pad = int(it["h_pad"])
            box = [int(it["x"]), y, int(it["x"]) + int(it["w"]), y + h_pad]
            draw.rounded_rectangle(box, radius=int(h_pad * 0.16),
                                   fill=accent if accent else (28, 28, 30))
            tw = int(it["font"].getlength(it["text"]))
            th = int(it["font"].size)
            draw.text(((box[0] + box[2]) // 2 - tw // 2, (box[1] + box[3]) // 2 - th // 2
                       - int(it["font"].size * 0.08)),
                      it["text"], font=it["font"], fill=(255, 255, 255))


def _tpl_magazine(canvas, subject, design, texts, text_color):
    """杂志版式：文字块整体落在画面一侧留白区，主体占据另一侧（物理分离）。

    与 hero 的差别：
    - 标题用衬线体、字号更大、行距更紧（编辑感）；
    - 卖点用「圆形描边对勾 + 文字」而非胶囊 chips；
    - CTA 为深色实心圆角按钮；
    - 主体不做多版位寻优，而是固定「文字在一侧、主体居中在另一侧」，构图更稳。
    """
    w, h = canvas.size
    mx = int(w * 0.075)
    my = int(h * 0.075)
    gutter = int(min(w, h) * 0.05)
    col_w = int(w * 0.40)

    # 主体可用比例：magazine 版式主体不追求大特写，0.55~0.72 更有呼吸感
    try:
        max_scale = float(design.get("_subject_scale") or 0.68)
    except (TypeError, ValueError):
        max_scale = 0.68
    max_foot = min(w, h) * max_scale

    # 文字在左 / 在右，取 text_area 的偏好（默认左，与主流杂志一致）
    side = str(design.get("text_area") or "left").lower()
    if side not in {"left", "right"}:
        side = "left"

    if side == "left":
        tx, tw = mx, col_w
    else:
        tx, tw = w - mx - col_w, col_w

    # 垂直居中文字块（先量后放）
    probe_h, items, cw = _magazine_text_layout(canvas, design, texts, tx, tw)
    ty = max(my, int((h - probe_h) / 2))
    free_x0 = (tx + tw + gutter) if side == "left" else mx
    free_x1 = (w - mx) if side == "left" else (tx - gutter)
    free_y0, free_y1 = my, h - my
    if free_x1 - free_x0 < 60:
        free_x0 = mx if side == "left" else tx + tw + gutter
        free_x1 = w - mx if side == "left" else w - mx

    tw_, th_ = _fit_size(subject.size, (free_x0, free_y0, free_x1, free_y1), max_foot)
    sx = int((free_x0 + free_x1) / 2 - tw_ / 2)
    sy = int((free_y0 + free_y1) / 2 - th_ / 2)
    design["_subject_box"] = list(_blit_subject(canvas, subject, sx, sy, (tw_, th_)))
    subject_rect = _xywh_to_rect(tuple(design["_subject_box"]))

    # 文字区配色：以实际落笔矩形取色（此时背景与主体已就位）
    tx0, ty0 = tx, ty
    tx1, ty1 = tx + max(int(cw), int(tw * 0.5)) + int(w * 0.02), ty + probe_h
    pad = max(6, int(min(w, h) * 0.01))
    region = (max(0, tx0 - pad), max(0, ty0 - pad), min(w, tx1 + pad), min(h, ty1 + pad))
    tc = _text_color_for(canvas, region)
    muted = tuple(int(c * 0.72 + 128 * 0.28) for c in tc)  # 次要文字：向中灰靠拢
    accent = _pick_accent(design, tc)

    _magazine_draw(canvas, items, ty, tc, muted, accent)
    text_rect = (tx0, ty0, min(tx1, tx0 + int(cw)), ty1)
    design["_text_box"] = list(text_rect)

    # 右上角品类标签（与主体、文字都不冲突时才绘制）
    tag = (design.get("_tag") or "").strip()
    if tag:
        font_tag = load_font(int(h * 0.017), bold=True)
        ttw = int(font_tag.getlength(tag))
        tag_box = (w - mx - ttw, int(h * 0.045), w - mx, int(h * 0.045) + int(font_tag.size * 1.3))
        if not _rects_hit(tag_box, subject_rect) and not _rects_hit(tag_box, text_rect):
            ImageDraw.Draw(canvas).text((tag_box[0], tag_box[1]), tag, font=font_tag, fill=muted)
            design["_tag_box"] = list(tag_box)


def _tpl_minimal(canvas, subject, design, texts, text_color):
    canvas.paste((255, 255, 255), (0, 0, canvas.width, canvas.height))
    w, h = canvas.size
    _paste_subject(canvas, subject, box_ratio=design.get("_subject_scale", 0.62), center=(0.5, 0.44))
    draw = ImageDraw.Draw(canvas)
    font_h = load_font(int(h * 0.045), bold=True, rtl=_is_rtl(texts["headline"]))
    y = int(h * 0.78)
    _draw_text_block(draw, texts["headline"], font_h, int(w * 0.12), y, int(w * 0.76), (30, 30, 30), align="center")


def _draw_cta_button(canvas, cta: str, box: tuple[int, int, int, int], fill: tuple[int, int, int] | None = None) -> None:
    """绘制圆角 CTA 按钮（填充 + 文字）。填充色优先取视觉方案强调色，默认红。"""
    x0, y0, x1, y1 = box
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=int((y1 - y0) * 0.4), fill=fill or (230, 57, 70))
    fb = load_font(int((y1 - y0) * 0.5), bold=True)
    cta_text = cta[:24]
    tw = int(fb.getlength(cta_text))
    th = int(fb.size)
    draw.text(((x0 + x1) // 2 - tw // 2, (y0 + y1) // 2 - th // 2 - int(fb.size * 0.1)), cta_text, font=fb, fill=(255, 255, 255))


TEMPLATES: dict[str, Any] = {
    "centered": _tpl_centered,
    "left_text": _tpl_left_text,
    "promo_badge": _tpl_promo_badge,
    "compare": _tpl_compare,
    "minimal": _tpl_minimal,
    "hero": _tpl_hero,
    "magazine": _tpl_magazine,
}


def compose_ad_image(
    subject_image_url: str | None,
    background_bytes: bytes | None,
    design: dict[str, Any],
    headline: str,
    subheadline: str,
    cta: str,
    size_preset: str,
    bullets: list[str] | None = None,
) -> str:
    """合成广告图并保存，返回图片 URL。"""
    size = resolve_size(size_preset)
    canvas = _load_background(background_bytes, size)

    # 主体图
    subject = _load_subject(subject_image_url)
    if subject is None:
        logger.warning("主体图加载失败，使用占位图")
        subject = Image.new("RGBA", (size[0] // 3, size[0] // 3), (200, 200, 200, 255))

    palette = design.get("palette") or ["#1D1D1F"]
    text_color = _text_color(palette)
    template_id = design.get("template_id", "centered")
    template = TEMPLATES.get(template_id, TEMPLATES["centered"])

    # 记录主体原始尺寸（版式调试 / 回归比对用）
    design["_subject_src"] = [subject.width, subject.height]

    # 尊重视觉设计给出的主体占比（product_placement.scale），钳制在 0.35-0.85；
    # hero 大特写海报会给 0.65-0.85，桌面/生活场景给 0.40-0.55
    try:
        raw_scale = float((design.get("product_placement") or {}).get("scale") or 0)
    except (TypeError, ValueError):
        raw_scale = 0.0
    if 0.35 <= raw_scale <= 0.85:
        design["_subject_scale"] = raw_scale

    texts = {
        "headline": headline,
        "subheadline": subheadline,
        "cta": cta,
        "bullets": bullets or [],
        "promo": design.get("promo_badge", "SALE"),
    }
    template(canvas, subject, design, texts, text_color)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return storage.save_bytes(buf.getvalue(), "ad.png")


def _text_color(palette: list[str]) -> tuple[int, int, int]:
    for c in palette:
        rgb = _hex_to_rgb(c)
        if 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] < 180:
            return rgb
    return (30, 30, 30)


def _load_background(background_bytes: bytes | None, size: tuple[int, int]) -> Image.Image:
    if background_bytes:
        try:
            img = Image.open(io.BytesIO(background_bytes)).convert("RGBA").resize(size, Image.LANCZOS)
            return img
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"背景图加载失败，使用渐变背景: {exc}")
    bg_bytes = make_gradient_background(size)
    return Image.open(io.BytesIO(bg_bytes)).convert("RGBA")


def _load_subject(subject_url: str | None) -> Image.Image | None:
    if not subject_url:
        return None
    try:
        path = storage.absolute_path(subject_url)
        if path.exists():
            return Image.open(path).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"主体图加载失败: {exc}")
    return None


# ---------- 图生图模式：整图背景 + 文字避让叠加 ----------
# 适用于模特实拍服装等不宜抠图的商品：产品保真由"图像编辑只改背景/杂物"保证，
# 合成器不再贴抠图主体，只负责把文案排到不影响主体的位置。


def _fit_cover(
    img: Image.Image,
    size: tuple[int, int],
    anchor: str = "center",
) -> Image.Image:
    """等比缩放后裁剪到目标尺寸（不拉伸变形）。

    anchor 控制裁剪窗口偏向：
    - center：居中裁剪（默认）
    - left  ：尽量保留画面**左侧**（文字排左侧时用，留住左侧留白）
    - right ：尽量保留画面**右侧**
    """
    tw, th = size
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    nw = max(tw, int(sw * scale + 0.5))
    nh = max(th, int(sh * scale + 0.5))
    img = img.resize((nw, nh), Image.LANCZOS)
    slack_x = nw - tw
    slack_y = nh - th
    if anchor == "left":
        x = 0
    elif anchor == "right":
        x = slack_x
    else:
        x = slack_x // 2
    y = slack_y // 2
    return img.crop((x, y, x + tw, y + th))


def _smart_fit_cover(
    img: Image.Image,
    size: tuple[int, int],
    text_side: str,
    design: dict[str, Any] | None = None,
) -> Image.Image:
    """把生成图适配到画布，**优先保住文字侧留白**（整图重塑链路的关键一步）。

    问题：AI 按提示词把主体放在一侧、留白在另一侧；但普通 cover 居中裁剪会把
    留白裁掉、把主体放大到几乎铺满整幅，导致后面排文字必然压主体。

    做法：先在**原图**上估计主体外接框，再扫描所有合法裁剪窗口，
    选「完全含住主体 + 文字侧余量最大」的窗口 → 留白被最大化保留，
    主体也不会被裁切。估计失败时按 center 退化（保持旧行为）。
    """
    tw, th = size
    sw, sh = img.size
    rect = _detect_subject_rect(img.convert("RGB"), design)
    if rect is None:
        return _fit_cover(img, size)

    scale = max(tw / sw, th / sh)
    nw = max(tw, int(sw * scale + 0.5))
    nh = max(th, int(sh * scale + 0.5))
    scaled = img.resize((nw, nh), Image.LANCZOS)
    sx0, sy0, sx1, sy1 = (int(v * scale + 0.5) for v in rect)

    side = text_side if text_side in {"left", "right"} else "left"
    # 水平方向：窗口必须含住主体
    x_lo = max(0, sx1 - tw)
    x_hi = min(nw - tw, sx0)
    if x_lo > x_hi:  # 主体比画布还宽，无法完全含住 → 退化为含住主体中心
        x_lo = x_hi = max(0, min(nw - tw, (sx0 + sx1 - tw) // 2))
    if side == "left":
        # 文字在左 → 尽量让窗口右移，把左侧留白留在画布里
        x = x_hi
    else:
        # 文字在右 → 尽量让窗口左移，把右侧留白留在画布里
        x = x_lo

    y_lo = max(0, sy1 - th)
    y_hi = min(nh - th, sy0)
    if y_lo > y_hi:
        y_lo = y_hi = max(0, min(nh - th, (sy0 + sy1 - th) // 2))
    # 垂直：优先含住主体顶部（产品完整比底部留白更重要）
    y = y_lo
    return scaled.crop((x, y, x + tw, y + th))


def _load_base_cover(
    image_bytes: bytes,
    size: tuple[int, int],
    text_side: str = "center",
    design: dict[str, Any] | None = None,
) -> Image.Image:
    """加载图生图结果并按 cover 方式适配画布（含留白感知智能裁剪）。

    当 AI 返回图与目标画布的**长宽比差异较大**（>6%）时，不再 cover 裁剪
    （会切掉 AI 精心布置的构图/留白），改用"等比缩放 + 模糊延展补边"把画面
    完整放进画布——保住构图的同时不出现黑边。
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    # 长宽比差异较大时，cover 裁剪会切掉 AI 已布置好的构图/留白 → 改用 contain 补边。
    sw, sh = img.size
    tw, th = size
    if sw > 0 and sh > 0:
        src_ratio = sw / sh
        dst_ratio = tw / th
        if abs(src_ratio - dst_ratio) / max(src_ratio, dst_ratio) > 0.06:
            return _fit_contain(img, size)
    if text_side in {"left", "right"}:
        return _smart_fit_cover(img, size, text_side, design)
    return _fit_cover(img, size)


def _fit_contain(
    img: Image.Image,
    size: tuple[int, int],
) -> Image.Image:
    """等比缩放后**完整放入**画布，四周用原图放大模糊填充（不裁剪、不留黑边）。

    用于 AI 返回图与目标长宽比不一致时，避免 cover 裁剪破坏已生成的构图。
    """
    tw, th = size
    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        return Image.new("RGBA", size, (0, 0, 0, 255))

    scale = min(tw / sw, th / sh)
    nw = max(1, int(sw * scale + 0.5))
    nh = max(1, int(sh * scale + 0.5))
    fitted = img.resize((nw, nh), Image.LANCZOS)

    # 背景：原图放大铺满 + 高斯模糊，保证补边是画面的延伸而非突兀色块
    bg_scale = max(tw / sw, th / sh)
    bw = max(tw, int(sw * bg_scale + 0.5))
    bh = max(th, int(sh * bg_scale + 0.5))
    bg = img.resize((bw, bh), Image.LANCZOS)
    bg = bg.crop(((bw - tw) // 2, (bh - th) // 2,
                  (bw - tw) // 2 + tw, (bh - th) // 2 + th))
    from PIL import ImageFilter
    bg = bg.filter(ImageFilter.GaussianBlur(radius=max(tw, th) * 0.02))

    x = (tw - nw) // 2
    y = (th - nh) // 2
    bg.alpha_composite(fitted, (x, y))
    return bg


def _overlap_area(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int] | None,
) -> int:
    """两个 (x0,y0,x1,y1) 矩形的相交面积（不相交为 0）。

    b 为 None 表示"主体位置未知"（主体框估计不可用）——此时无法判定相交，
    统一返回 0，让排版按"无冲突"处理（宁可排版好看，也不要因未知而误判）。
    """
    if b is None:
        return 0
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    return iw * ih if (iw > 0 and ih > 0) else 0


def _prior_from_design(
    w: int, h: int, design: dict[str, Any] | None
) -> tuple[float, float, float, float]:
    """由 LLM 给出的 `product_placement.position` + `text_area` 构造**方向性场景先验框**。

    历史事故（任务 #3）：LLM 明确写了 `position="center-lower"`、`text_area="bottom"`，
    但旧实现对任何图都套"画面正中 0.62×0.62"的先验，把主体框硬拉到纵向居中，
    两侧可用的干净栏被算成"很窄"，于是文字被迫挤进顶部小角落。

    现在：按 position 的关键词（left/right/top/bottom/center）把先验框**偏向对应方位**，
    text_area 作为兜底（文字在哪侧，主体就先验偏向反侧）。position 缺失时才退回居中。
    """
    pos = ""
    if design:
        pp = design.get("product_placement")
        if isinstance(pp, dict):
            pos = str(pp.get("position") or "").lower()
        if not pos:
            pos = str(design.get("composition") or "").lower()
    side = str((design or {}).get("text_area") or "").lower()

    # 主体框基础占比（比旧 0.62 明显收窄）：整图叠加链路的文字只需"避开"主体，
    # 框给太大反而把可用的干净区算没了（任务 #3 的文字被逼进角落就与此有关）。
    bw = 0.50
    bh = 0.50
    cx, cy = 0.5, 0.5

    if any(k in pos for k in ("left",)):
        cx = 0.34
    elif any(k in pos for k in ("right",)):
        cx = 0.66
    elif "center" in pos:
        cx = 0.5

    if "lower" in pos or "bottom" in pos:
        cy = 0.64
    elif "upper" in pos or "top" in pos:
        cy = 0.36
    elif "center" in pos:
        cy = 0.5

    # position 完全缺失时，用 text_area 反推
    if not pos:
        if side == "left":
            cx = 0.68
        elif side == "right":
            cx = 0.32
        elif side == "top":
            cy = 0.62
        elif side == "bottom":
            cy = 0.38

    x0 = (cx - bw / 2) * w
    x1 = (cx + bw / 2) * w
    y0 = (cy - bh / 2) * h
    y1 = (cy + bh / 2) * h
    return (x0, y0, x1, y1)


def _detect_subject_rect(
    canvas: Image.Image, design: dict[str, Any] | None = None
) -> tuple[int, int, int, int] | None:
    """估计整图画面中"产品/人物主体"的外接框 (x0,y0,x1,y1)，供文字避让。

    ## 为什么不能只靠 rembg

    早期实现：rembg 出 alpha → 按列/行覆盖率取边界。这在**抠图贴图链路**
    （产品图 + 纯背景）上没问题，但在**整图重塑链路**（AI 直接画出的完整场景）
    上会彻底失效：整图重塑的产物是"已布置好的实景"，rembg 分的是"前景/背景"，
    而带道具的室内实景（挂衣杆、干花、桌面、钱包）里**道具同样算前景**。

    实测（828×828 耳机场景图）：总体 alpha 覆盖率 53.9%，阈值从 0.06 提到 0.30
    主体框仍停在 67%~73% 画幅；用 α 加权矩（质心±1.2σ）虽能把框收到 27%，
    但一旦画面下部被遮挡/有干扰，矩会整体偏移到上部道具上（实测偏移到挂衣杆，
    于是文字被排到耳机上）。**结论：单靠 rembg 无法在实景图里稳定指认"产品"。**

    ## 现在的做法：方向性场景先验 + α 能量的加权融合

    这类场景图的构图有强先验，而且 **LLM 已经把先验明写出来了**
    （`product_placement.position` / `text_area`）。于是：
      1. 用 α 加权矩求"图像意义上的显著区"（对纯色/简单背景依然准确）；
      2. 与**按 position/text_area 定向的**场景先验框（见 `_prior_from_design`）融合；
      3. 融合权重偏向先验（0.6），避免被上部道具带偏。

    **无法可靠估计时返回 None**（而不是"保守大框"）——大框会让任何版位都被判
    "文字压主体"，把干净版位误排到后面。调用方拿到 None 时按"主体位置未知"处理。
    """
    w, h = canvas.size
    try:
        import numpy as np
        from rembg import new_session, remove

        session = new_session("u2net")
        cutout = remove(canvas.convert("RGB"), session=session)
        alpha = np.asarray(cutout.convert("RGBA").split()[3]).astype(np.float32) / 255.0

        prior = _prior_from_design(w, h, design)

        total = float(alpha.sum())
        if total <= 1.0:  # 全透明 → 没抠出任何东西，直接用先验
            return (int(prior[0]), int(prior[1]), int(prior[2]), int(prior[3]))

        cols = alpha.sum(axis=0)
        rows = alpha.sum(axis=1)
        xs = float((cols * np.arange(w)).sum() / total)
        ys = float((rows * np.arange(h)).sum() / total)
        sx = float(np.sqrt((cols * (np.arange(w) - xs) ** 2).sum() / total))
        sy = float(np.sqrt((rows * (np.arange(h) - ys) ** 2).sum() / total))
        if sx < 1.0 or sy < 1.0:
            return (int(prior[0]), int(prior[1]), int(prior[2]), int(prior[3]))

        k = 1.2
        a_box = (xs - k * sx, ys - k * sy, xs + k * sx, ys + k * sy)

        # ---- 与定向场景先验融合（权重偏向先验，抗道具干扰）----
        wp = 0.6
        x0 = wp * prior[0] + (1 - wp) * a_box[0]
        y0 = wp * prior[1] + (1 - wp) * a_box[1]
        x1 = wp * prior[2] + (1 - wp) * a_box[2]
        y1 = wp * prior[3] + (1 - wp) * a_box[3]

        x0, x1 = int(max(0, x0)), int(min(w, x1))
        y0, y1 = int(max(0, y0)), int(min(h, y1))

        # 面积过小 → 噪声；几乎铺满整幅 → 估计失效，均视为不可靠
        if (x1 - x0) * (y1 - y0) < w * h * 0.02:
            raise ValueError("主体框过小")
        if (x1 - x0) > w * 0.94 and (y1 - y0) > h * 0.94:
            raise ValueError("主体框几乎铺满画布，视为不可靠")
        return (x0, y0, x1, y1)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"图生图主体框估计不可用，跳过文字相交判定: {exc}")
        return None


def _tpl_hero_overlay(
    canvas: Image.Image,
    design: dict[str, Any],
    texts: dict[str, Any],
    subject_rect: tuple[int, int, int, int] | None,
) -> None:
    """图生图模式的文字排版：画面已含主体，只选择不压主体的版位叠加文案。

    版位优先级：视觉方案指定的 text_area > 四角 > 四边 > 居中偏移；
    若所有版位都会与主体相交，选**相交面积最小**的，并在文字区加半透明衬底保证
    可读——同时把相交比例写进 design，供版式调试与回归比对。

    为什么候选要多：早期只试 top/left/right/bottom 四个"通栏"版位。当主体居中且
    纵向跨度大（耳机、音箱、人像）时，四个版位全都会与主体相交，只能退化成"选相交
    最小的 + 加衬底"，结果就是文字直接压在耳罩上（实测 828×828 耳机图）。
    这里改为先试**内容宽度自适应**的窄栏版位（角落优先），只有窄栏实在放不下才
    退到通栏；并且用文字**真实内容框**（而非整个栏宽）算相交面积。
    """
    w, h = canvas.size
    mx = int(w * 0.06)
    my = int(h * 0.05)
    wide_w = w - 2 * mx
    gap = int(w * 0.03)

    # 侧栏宽度自适应主体留白：主体左右两侧的空白各能放多宽的文字栏
    if subject_rect is not None:
        left_free = max(0, subject_rect[0] - gap - mx)
        right_free = max(0, (w - subject_rect[2]) - gap - mx)
    else:
        left_free = right_free = wide_w

    def _col_w(free: int) -> int:
        """在主体留白内尽量宽地排文字栏。

        阈值从画宽 30% 放宽到 16%：30% 是为"放得下通栏级标题"设的，但线上耳机图
        实测主体左右留白只有 ~17% 画宽——那是**唯一真正干净**的区域，却因为够不着
        30% 被整个丢掉，候选清空后退化成"最小字号 + 压主体"。窄栏配合小一点的字号
        照样能排出干净且可读的文案，不该在版位生成阶段就把它排除。
        """
        if free >= int(w * 0.16):
            return min(wide_w, free)
        return 0  # 留白太窄（< 16% 画宽），该侧确实放不了

    # 候选生成：(zone, x, 宽度, 顶部 y)。宽度分三档——通栏 / 半栏 / 侧栏，
    # 配合下方的字号缩放，形成"位置 × 尺寸"的二维搜索空间。
    def _zone_specs() -> list[tuple[str, int, int, int]]:
        specs: list[tuple[str, int, int, int]] = []
        half = int(w * 0.50)
        specs += [
            ("top", mx, wide_w, my),
            ("top-left", mx, half, my),
            ("top-right", w - mx - half, half, my),
        ]
        cw_l, cw_r = _col_w(left_free), _col_w(right_free)
        if cw_l:
            specs.append(("left", mx, cw_l, my))
        if cw_r:
            specs.append(("right", w - mx - cw_r, cw_r, my))
        specs += [
            ("bottom", mx, wide_w, 0),
            ("bottom-left", mx, half, 0),
            ("bottom-right", w - mx - half, half, 0),
        ]
        return specs

    pref = str(design.get("text_area") or "top").lower()
    order = {"top": 0, "left": 1, "right": 2, "bottom": 3,
             "top-left": 4, "top-right": 5, "bottom-left": 6, "bottom-right": 7}

    # ── text_area 硬约束（P0 修复）─────────────────────────────────────────
    # 历史事故：LLM 明确给出 text_area="bottom"、scene_prompt 也写明"留白在下方"，
    # 但渲染器只把 text_area 当作"同分排序偏好"（sort_key 第 7 档），结果被"贴顶窄栏
    # 恰好干净"抢走 → 文字排到左上角、压在浅色木纹上，与提示词完全矛盾。
    #
    # 现在：把 LLM 指定的方位升级为**第一优先硬约束**——先只在"该侧"的版位里挑，
    # 只有该侧**完全放不下**（连最小字号都不干净）时才放开到其他侧，并在设计里
    # 记录降级，便于排查。
    _SIDE_ZONES: dict[str, set[str]] = {
        "top": {"top", "top-left", "top-right"},
        "bottom": {"bottom-left", "bottom-right", "bottom"},
        "left": {"left", "top-left", "bottom-left"},
        "right": {"right", "top-right", "bottom-right"},
    }
    pref_zones = _SIDE_ZONES.get(pref, set(order.keys()))

    # 字号缩放档位：从大往小试，取"不压主体"里最大的那档。
    # 为什么要有 >1.0 的档位：基准字号（标题 h*0.075 等）在 2000px 画布上偏小，
    # 实测整块只占画高 17%，留白区绰绰有余——不给放大档位，文字就只能停在
    # "缩略图上读不清"的大小。搜索是"从大到小"，故大档位在前，命中即收手。
    # 必须缩小而不是"换更窄的栏"——栏越窄换行越多、块越高（实测窄栏反而更压主体）。
    SCALES = (1.60, 1.38, 1.20, 1.0, 0.88, 0.78, 0.68, 0.58, 0.50)

    # 内容精简档位：居中主体会让上下留白只有 ~12~16% 画高，此时即使缩到最小字号，
    # "大标题 + 副标题 + 3 个卖点徽章 + CTA"也塞不进留白。与其压住产品或缩到
    # 不可读，不如**按重要性裁剪**：先砍卖点徽章，再砍副标题，保住大标题与 CTA
    # ——广告的核心信息量几乎全在这两项，砍掉徽章对说服力的损伤远小于压住产品。
    def _trim(level: int) -> dict[str, Any]:
        t = dict(texts)
        if level >= 1:
            t["bullets"] = []
        if level >= 2:
            t["subheadline"] = ""
        return t

    # 搜索策略：把**所有**"完全不压主体"的干净候选全部收集起来，最后统一排序择优。
    #
    # 为什么不能"命中第一个干净方案就收手"：原实现按 (level, zone, scale) 顺序遍历，
    # 一旦某个版位在小字号下"干净"就立刻 break，导致**宁可要小字号也不要大字号**。
    # 线上实测：耳机主体铺满画面时，左上角窄栏在小字号下干净 → 被选中，
    # 标题只有约 75px（画高 3.75%），缩略图上根本读不清；而同一张画的右侧留白
    # 其实能容纳大得多的字号，只因排序在前被提前退出而从未被考虑。
    #
    # **纵向滑动**：早先每个版位只试"贴顶(y=my) / 贴底(底部版位)"两个纵坐标。当主体
    # 纵向跨度大时，顶部干净带可能只有 ~8% 画高，贴顶排布必然压主体；但把文字**往下
    # 挪一点或贴着主体上沿**往往就能腾出空间。这里对每个版位的候选纵坐标做一组采样
    # （贴顶、贴主体上沿、垂直居中、贴底），配合字号档形成 (纵坐标 × 字号) 二维搜索，
    # 显著扩大"干净且字号大"候选的命中率。
    candidates: list[dict[str, Any]] = []
    # 主体下沿基准（bottom 版位把文字排在主体下沿之下）——无主体框时为 None
    subj_bottom_anchor = (subject_rect[3] + gap) if subject_rect is not None else None
    # 两轮：先只在 text_area 指定侧的版位里找；找不到干净方案才放开到全部版位。
    for zone_filter in (pref_zones, None):
        candidates = []
        for level in (0, 1, 2):
            t = _trim(level)
            level_cands: list[dict[str, Any]] = []
            for zone, tx, tw, ty in _zone_specs():
                if zone_filter is not None and zone not in zone_filter:
                    continue
                if tw < int(w * 0.16):      # 窄于可读下限，放弃
                    continue
                for sc in SCALES:
                    th, items, cw = _hero_text_layout(canvas, design, t, tx, tw, scale=sc)
                    # 候选纵坐标：由**版位方向**决定锚点集合。
                    # ⚠️ 不能所有版位共用同一组锚点：bottom 版位若也允许"贴主体上沿"
                    # 或"垂直居中"锚点，会把文字送到画面上半部——明明要求排底部，
                    # 结果排到了顶部（P0 text_area 修复过程中实测到的自相矛盾）。
                    bottom_zone = zone.startswith("bottom")
                    top_zone = zone.startswith("top")
                    if bottom_zone:
                        # 只考虑下半部：贴底 / 贴主体下沿之下 / 垂直居中偏下
                        ys = {h - my - th,
                              subj_bottom_anchor - th if subj_bottom_anchor is not None else -1,
                              max(my, (h - th) // 2)}
                    elif top_zone:
                        # 只考虑上半部：贴顶 / 贴主体上沿之上 / 垂直居中偏上
                        ys = {my,
                              (subject_rect[1] - gap - th) if subject_rect is not None else -1,
                              max(my, (h - th) // 4)}
                    else:
                        # 左右侧栏：纵向自由，取贴顶/居中/贴底/贴主体上沿
                        ys = {my, h - my - th, max(my, (h - th) // 2)}
                        if subject_rect is not None:
                            ys.add(subject_rect[1] - gap - th)
                    ys = {v for v in ys if v >= 0}
                    for yy in sorted(ys):
                        if yy < my or yy + th > h - int(h * 0.02):
                            continue
                        trect = (tx, yy, tx + cw, yy + th)
                        if _overlap_area(trect, subject_rect) <= 0:
                            level_cands.append({"zone": zone, "x": tx, "y": yy, "w": tw, "h": th,
                                                "cw": cw, "items": items, "scale": sc,
                                                "level": level, "overlap": 0})
            if level_cands:
                candidates = level_cands
                break   # 该内容档已有干净方案，不再放宽内容
        if candidates:
            # 记录 text_area 约束是否被满足（供版式调试）
            design["_text_area_honored"] = zone_filter is not None
            break
    else:
        design["_text_area_honored"] = False

    if not candidates:
        # 实在挤不下（主体几乎铺满画布）：退到"最小字号 + 最简内容 + 重叠最小"的方案。
        best_ov = None
        for zone, tx, tw, ty in _zone_specs():
            if tw < int(w * 0.16):
                continue
            th, items, cw = _hero_text_layout(canvas, design, _trim(2), tx, tw, scale=0.50)
            yy = ty if ty else h - my - th
            yy = max(my, min(yy, h - my - th))
            trect = (tx, yy, tx + cw, yy + th)
            ov = _overlap_area(trect, subject_rect)
            if best_ov is None or ov < best_ov:
                best_ov = ov
                candidates = [{"zone": zone, "x": tx, "y": yy, "w": tw, "h": th,
                               "cw": cw, "items": items, "scale": 0.50,
                               "level": 2, "overlap": ov}]

    # 可读性下限：标题实际字号低于画高的 4.5% 时，缩略图/手机端基本读不清。
    # 排序时用它做一档"是否达到可读"的优先，避免把"极小字号但位置最合意"的方案选中。
    #
    # 注意：这里算的是**放大后**的实际字号（基准比例 × 候选 scale）。本链路（整图叠加）
    # 的主体已经在画面里，文字只需避开、不与主体争画布，所以默认就该往上放大取大档；
    # SCALES 里 >1.0 的档位正是为此保留。基准比例仍是 _hero_text_layout 的 0.075，
    # 不要为了放大而改动基准值（会连带改变抠图贴图链路 _tpl_hero 的主体占位）。
    legible_floor = h * 0.045
    base_title_ratio = 0.075

    def _has_orphan_line(c: dict[str, Any]) -> int:
        """标题最后一行只剩 1~2 个字符 → 排版孤字（如"沉浸式/听觉盛/宴"）。

        孤字是明显的排版缺陷：既难看又浪费一整行高度。窄栏 + 大字号时极易触发。
        返回 0 表示没有孤字（好），1 表示有孤字（差），作为排序里的一档惩罚。
        优先读排版阶段存下的 `lines`（均衡换行后的结果），保证与最终绘制一致。
        """
        for it in c.get("items") or []:
            if it.get("kind") != "text" or it.get("stroke") != 1:
                continue    # 只检查标题行（stroke=1 是标题的绘制标记）
            lines = it.get("lines")
            if not lines:
                font = it.get("font")
                if font is None:
                    continue
                lines = _wrap_text_balanced(
                    str(it.get("text") or ""), font, int(it.get("max_w") or c["w"]))
            if len(lines) >= 2 and len(lines[-1].strip()) <= 1:
                return 1
        return 0

    def sort_key(c: dict[str, Any]) -> tuple[int, int, int, int, int, float, int, int]:
        # 用**真实内容框**（cw）算相交，而不是整个栏宽——否则窄内容配宽栏会
        # 被误判成"压主体"，把本来干净的位置排到后面
        cw = int(c.get("cw") or c["w"])
        trect = (c["x"], c["y"], c["x"] + cw, c["y"] + c["h"])
        ov = _overlap_area(trect, subject_rect)
        clean = 0 if ov <= 0 else 1
        # 标题实际字号（基准比例 × 本候选采用的放大档位）
        title_px = max(14, int(h * base_title_ratio * float(c.get("scale", 1.0))))
        small = 0 if title_px >= legible_floor else 1
        orphan = _has_orphan_line(c)
        # 排序优先级：① 干净 ② 重叠面积 ③ 标题无孤字 ④ **字号达到可读下限**
        # ⑤ 保留内容更多（level 小）⑥ 字号更大 ⑦ 视觉方案指定版位 ⑧ 版位固定顺序
        #
        # 为什么"可读"要排在"内容更多"之前：卖点徽章被裁掉只是信息量减少，
        # 而字号小到读不清等于文案完全失效——后者对广告的伤害大得多。
        # 为什么"孤字"排在"字号"之前：孤字是硬伤（一眼可见的排版事故），
        # 而字号小一档只是可读性略降，两者不可同日而语。
        return (clean, ov, orphan, small, int(c.get("level", 0)),
                -float(c.get("scale", 1.0)),
                0 if c["zone"] == pref else 1, order.get(c["zone"], 9))

    candidates.sort(key=sort_key)
    best = candidates[0]

    # 记录实际采用的版位与字号档，供版式调试与回归比对（此前这些信息只活在局部变量里，
    # 线上排查"文案为什么这么小"时无从下手）。
    design["_text_zone"] = best.get("zone")
    design["_text_scale"] = round(float(best.get("scale", 1.0)), 3)
    design["_text_level"] = int(best.get("level", 0))

    tx0, ty0 = best["x"], best["y"]
    cw = int(best.get("cw") or best["w"])
    tx1, ty1 = tx0 + cw, ty0 + best["h"]
    pad = max(6, int(min(w, h) * 0.012))
    region = (max(0, tx0 - pad), max(0, ty0 - pad), min(w, tx1 + pad), min(h, ty1 + pad))
    trect = (tx0, ty0, tx1, ty1)
    overlap = _overlap_area(trect, subject_rect)

    # 先定文字色（按区域平均亮度选深/浅），再判**是否需要衬底**：
    # 衬底不再只服务于"压主体"，而是服务于"对比度不足"——白字压在浅色木纹/高光上
    # 同样需要衬底（线上任务 #3 的事故）。仅当对比度达标的均匀背景才不加衬底。
    tc = _text_color_for(canvas, region)
    scrim = _needs_scrim(canvas, region, tc) if overlap <= 0 else 1
    if scrim:
        # 用**定向渐变蒙版**（贴边羽化）而不是圆角色块：更接近商业海报的整面压暗/提亮，
        # 也避免"画面中央突然出现一个灰框"的廉价感。
        _draw_directional_scrim(canvas, str(best.get("zone") or "top"), trect)
        design["_text_scrim"] = True
    if overlap > 0:
        # 记录相交程度（版式调试 / 回归比对用；轻微擦边不拦）
        design["_text_subject_overlap"] = overlap
        if subject_rect is not None:
            subj_area = max(1, (subject_rect[2] - subject_rect[0]) * (subject_rect[3] - subject_rect[1]))
            design["_text_subject_overlap_ratio"] = round(overlap / subj_area, 4)

    # 衬底已经改变了文字落笔处的底色 → 在**衬底之后**重新取色，避免"照着旧底色选字色"
    if scrim:
        tc = _text_color_for(canvas, region)
    accent = _pick_accent(design, tc)
    _hero_draw(canvas, best["items"], ty0, tc, accent)

    # _text_box 用**真实内容框**（含 CTA 按钮宽度），不含右侧空白——
    # 早期写成整个栏宽会把一大片空白算成"文字占用"，既误判相交，也让
    # 品类标签/图标行被无谓地挤掉。
    design["_text_box"] = [tx0, ty0, tx1, ty1]
    text_rect = (tx0, ty0, tx1, ty1)

    # 右上角品类标签：与主体、文字都不冲突时才绘制
    tag = (design.get("_tag") or "").strip()
    if tag:
        font_tag = load_font(int(h * 0.020), bold=True)
        ttw = int(font_tag.getlength(tag))
        tag_box = (w - mx - ttw, int(h * 0.045), w - mx, int(h * 0.045) + int(font_tag.size * 1.3))
        if not _rects_hit(tag_box, subject_rect) and not _rects_hit(tag_box, text_rect):
            ImageDraw.Draw(canvas).text((tag_box[0], tag_box[1]), tag, font=font_tag, fill=tc)
            design["_tag_box"] = list(tag_box)

    # 底部场景图标行：既不压主体也不压文字时才绘制
    bullets = [b for b in texts.get("bullets", []) if b and b.strip()]
    row_items = design.get("_scenes") or bullets[:4]
    if row_items:
        row_box = (0, int(h * 0.83), w, int(h * 0.98))
        if not _rects_hit(row_box, subject_rect) and not _rects_hit(row_box, text_rect):
            _draw_feature_row(canvas, row_items[:4], tc, accent=accent, icon=design.get("_icon", "check"))


def _tpl_magazine_overlay(
    canvas: Image.Image,
    design: dict[str, Any],
    texts: dict[str, Any],
    subject_rect: tuple[int, int, int, int] | None,
) -> None:
    """图生图模式的杂志排版：画面已含完整主体，文字落到与主体不相交的一侧留白区。

    与 hero_overlay 的差别：
    - 优先选「与主体零相交」的一侧；两侧都相交时不再用大面积衬底盖住画面，
      而是退化为**局部渐变蒙版**（仅文字所在的一条边），保留画面通透感；
    - 用 magazine 自己的排版（衬线大标题 / 图标卖点 / 深色 CTA）。
    """
    w, h = canvas.size
    mx = int(w * 0.075)
    my = int(h * 0.075)
    # 可读性下限：低于此宽度标题会挤成多行，宁可改用其他版位
    min_col = int(w * 0.24)
    col_w_max = int(w * 0.46)

    pref = str(design.get("text_area") or "left").lower()
    if pref not in {"left", "right", "top", "bottom"}:
        pref = "left"

    # 列宽自适应：文字侧实际可用留白 = 主体框到该侧边缘的距离（留出安全间距）。
    # 让文字严格限制在主体之外，而不是套固定宽度硬压上去。
    # 留白不足时可读性下限时，该版位会被判为"容纳不下"，让排序把机会给另一侧。
    gap = int(w * 0.035)
    side_avail = {
        "left": max(0, (subject_rect[0] - gap - mx)) if subject_rect else (w - 2 * mx),
        "right": max(0, (w - subject_rect[2] - gap - mx)) if subject_rect else (w - 2 * mx),
    }
    col_w = {z: min(col_w_max, side_avail[z]) for z in ("left", "right")}

    # 候选：左右优先（杂志风），再看视觉方案偏好
    order = {"left": 0, "right": 1, "top": 2, "bottom": 3}

    def _cand(zone: str) -> dict[str, Any]:
        if zone in ("left", "right"):
            tw = col_w[zone]
            if tw < min_col:
                return {"zone": zone, "fits": False}
            tx = mx if zone == "left" else w - mx - tw
            # 垂直避让：若主体占据中部，尝试把文字块放到主体上方或下方
            bh, items, cw = _magazine_text_layout(canvas, design, texts, tx, tw)
            ty = max(my, int((h - bh) / 2))
            best_ty, best_ov = ty, _overlap_area((tx, ty, tx + tw, ty + bh), subject_rect)
            if subject_rect is not None and best_ov > 0:
                for cand_y in (
                    my,
                    int(h - my - bh),
                    max(my, subject_rect[1] - bh - int(h * 0.02)),
                    min(h - my - bh, subject_rect[3] + int(h * 0.02)),
                ):
                    if cand_y < my or cand_y + bh > h - my:
                        continue
                    ov = _overlap_area((tx, cand_y, tx + tw, cand_y + bh), subject_rect)
                    if ov < best_ov:
                        best_ty, best_ov = cand_y, ov
            rect = (tx, best_ty, tx + tw, best_ty + bh)
            return {"zone": zone, "fits": True, "x": tx, "y": best_ty, "w": tw,
                    "h": bh, "cw": cw, "items": items, "rect": rect}
        # top / bottom：通栏，但若主体在顶端/底端则收窄，让文字避开主体
        tx, tw = mx, w - 2 * mx
        # 主体横向占位（只取主体所在的那一段），把顶部/底部条带限制在主体之外
        if subject_rect is not None:
            sl, sr = subject_rect[0], subject_rect[2]
            gap2 = int(w * 0.035)
            # 主体靠左 → 文字条带右移；主体靠右 → 条带左移；主体居中 → 用较宽一侧
            if sl > mx + gap2 and (w - sr) >= (sl - mx):
                tx, tw = mx, max(0, sl - gap2 - mx)
            elif (w - sr) > mx + gap2:
                tx, tw = sr + gap2, max(0, w - mx - sr - gap2)
        if tw < min_col:
            tx, tw = mx, w - 2 * mx  # 收窄后放不下 → 保持通栏（走蒙版兜底）
        bh, items, cw = _magazine_text_layout(canvas, design, texts, tx, tw)
        ty = my if zone == "top" else h - my - bh
        rect = (tx, ty, tx + tw, ty + bh)
        return {"zone": zone, "fits": True, "x": tx, "y": ty, "w": tw, "h": bh,
                "cw": cw, "items": items, "rect": rect}

    cands = [c for c in (_cand(z) for z in ("left", "right", "top", "bottom")) if c.get("fits")]
    if not cands:
        # 极端情况（左右都放不下且通栏也异常）：退回通栏
        cands = [_cand("top")]

    def sort_key(c: dict[str, Any]) -> tuple[int, int, int]:
        ov = _overlap_area(c["rect"], subject_rect)
        return (0 if ov <= 0 else 1, 0 if c["zone"] == pref else 1, order[c["zone"]])

    cands.sort(key=sort_key)
    best = cands[0]
    tx0, ty0 = best["x"], best["y"]
    # 用**真实内容宽度**（cw）而不是整列宽：magazine 标题左对齐，列右侧常有大片空白，
    # 按整列宽算相交会把空白误判成"压主体"。
    cw = int(best.get("cw") or best["w"])
    tx1, ty1 = tx0 + cw, ty0 + best["h"]
    rect = (tx0, ty0, tx1, ty1)
    overlap = _overlap_area(rect, subject_rect)

    if overlap > 0:
        # 无法完全避开主体：只在文字所在的一侧加**定向渐变蒙版**（不是整块灰板），
        # 保住画面通透感，同时保证文字可读。
        _draw_directional_scrim(canvas, best["zone"], rect)
        design["_text_scrim"] = True
        # 记录相交程度（版式调试 / 回归比对用；同 hero_overlay）
        design["_text_subject_overlap"] = overlap
        if subject_rect is not None:
            subj_area = max(1, (subject_rect[2] - subject_rect[0]) * (subject_rect[3] - subject_rect[1]))
            design["_text_subject_overlap_ratio"] = round(overlap / subj_area, 4)

    region = (max(0, tx0 - mx // 2), max(0, ty0 - mx // 2),
              min(w, tx1 + mx // 2), min(h, ty1 + mx // 2))
    tc = _text_color_for(canvas, region)
    muted = tuple(int(c * 0.72 + 128 * 0.28) for c in tc)
    accent = _pick_accent(design, tc)
    _magazine_draw(canvas, best["items"], ty0, tc, muted, accent)

    design["_text_box"] = [tx0, ty0, tx1, ty1]
    text_rect = (tx0, ty0, tx1, ty1)

    # 右上角品类标签（不压主体与文字时才画）
    tag = (design.get("_tag") or "").strip()
    if tag:
        font_tag = load_font(int(h * 0.017), bold=True)
        ttw = int(font_tag.getlength(tag))
        tag_box = (w - mx - ttw, int(h * 0.045), w - mx, int(h * 0.045) + int(font_tag.size * 1.3))
        if not _rects_hit(tag_box, subject_rect) and not _rects_hit(tag_box, text_rect):
            ImageDraw.Draw(canvas).text((tag_box[0], tag_box[1]), tag, font=font_tag, fill=muted)
            design["_tag_box"] = list(tag_box)


def _draw_directional_scrim(
    canvas: Image.Image,
    zone: str,
    rect: tuple[int, int, int, int],
) -> None:
    """定向渐变蒙版：覆盖**整个文字块**（保证可读），仅在块的外侧边缘羽化淡出。

    关键：蒙版必须在文字块范围内保持接近满强度，否则低处的文字会因为
    衰减过早而糊在背景里（第一版按整幅宽度线性衰减，导致第三行卖点几乎不可见）。
    支持 8 个版位（top/bottom/left/right + 四个角落），角落版位沿对应两轴羽化。
    """
    w, h = canvas.size
    x0, y0, x1, y1 = (max(0, rect[0]), max(0, rect[1]), min(w, rect[2]), min(h, rect[3]))
    lum = _region_luminance(canvas, (x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))
    dark = lum > 128  # 亮底 → 加暗蒙版；暗底 → 加亮蒙版
    base = (0, 0, 0) if dark else (255, 255, 255)
    strength = 150 if dark else 140

    pad = int(min(w, h) * 0.035)          # 蒙版相对文字块的外扩
    feather = int(min(w, h) * 0.10)       # 外侧羽化带宽度
    steps = 64

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    def _band(start: int, end: int, vertical: bool, alpha_from: int, alpha_to: int) -> None:
        """沿 x（vertical=False）或 y（vertical=True）画一段线性透明度渐变条。"""
        span = max(1, end - start)
        for i in range(steps):
            t = i / steps
            a = int(alpha_from + (alpha_to - alpha_from) * t)
            pos = start + int(span * t)
            nxt = start + int(span * (t + 1 / steps)) + 1
            if vertical:
                od.rectangle([0, pos, w, nxt], fill=(*base, a))
            else:
                od.rectangle([pos, 0, nxt, h], fill=(*base, a))

    left = zone in ("left", "top-left", "bottom-left")
    right = zone in ("right", "top-right", "bottom-right")
    top = zone in ("top", "top-left", "top-right")
    bottom = zone in ("bottom", "bottom-left", "bottom-right")

    # 实心核心区（含 padding，不含羽化）
    cx0 = 0 if left else max(0, x0 - pad)
    cx1 = w if right else min(w, x1 + pad)
    cy0 = 0 if top else max(0, y0 - pad)
    cy1 = h if bottom else min(h, y1 + pad)
    od.rectangle([cx0, cy0, cx1, cy1], fill=(*base, strength))

    # 外侧羽化：从核心区边缘向外线性衰减
    if left:
        _band(cx1, min(w, cx1 + feather), False, strength, 0)
    if right:
        _band(max(0, cx0 - feather), cx0, False, 0, strength)
    if top:
        _band(cy1, min(h, cy1 + feather), True, strength, 0)
    if bottom:
        _band(max(0, cy0 - feather), cy0, True, 0, strength)

    # 内侧（画面反向）也做一次短羽化，避免出现硬边
    inner = int(min(w, h) * 0.06)
    if left:
        _band(0, min(w, inner), False, 0, strength)
    if right:
        _band(max(0, w - inner), w, False, strength, 0)
    if top:
        _band(0, min(h, inner), True, 0, strength)
    if bottom:
        _band(max(0, h - inner), h, True, strength, 0)

    canvas.alpha_composite(overlay)


def compose_ad_from_image(
    base_image_bytes: bytes,
    design: dict[str, Any],
    headline: str,
    subheadline: str,
    cta: str,
    size_preset: str,
    bullets: list[str] | None = None,
) -> str:
    """图生图模式的合成：编辑生成的整图直接作为广告画面，仅叠加文案层（不贴抠图主体）。

    与 `compose_ad_image`（抠图贴图）互为兜底：
    - 产品保真：由图像编辑指令"只改背景/去杂物、不改产品"保证；
    - 文字避让：对生成图做主体外接框估计，文案排在框外；无法避开时加衬底；
    - 不写入 `_subject_src`（主体不是贴上去的，等比校验不适用）。
    """
    size = resolve_size(size_preset)
    # 智能裁剪：按视觉方案的文字侧别保留对应留白（整图重塑链路必须做，
    # 否则 AI 生成的"主体在一侧、留白在另一侧"会被居中裁剪破坏）
    _side = str(design.get("text_area") or "").lower()
    if _side not in {"left", "right"}:
        _side = "center"
    canvas = _load_base_cover(base_image_bytes, size, text_side=_side, design=design)
    design["_compose_mode"] = "imagegen"

    texts = {
        "headline": headline,
        "subheadline": subheadline,
        "cta": cta,
        "bullets": bullets or [],
        "promo": design.get("promo_badge", "SALE"),
    }

    subject_rect = _detect_subject_rect(canvas, design)
    if subject_rect is not None:
        design["_subject_box"] = [
            subject_rect[0], subject_rect[1],
            subject_rect[2] - subject_rect[0], subject_rect[3] - subject_rect[1],
        ]
    # 按 template_id 选择整图叠加版式：magazine（杂志排版）或 hero（默认避让叠加）
    if str(design.get("template_id") or "").lower() == "magazine":
        _tpl_magazine_overlay(canvas, design, texts, subject_rect)
    else:
        _tpl_hero_overlay(canvas, design, texts, subject_rect)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return storage.save_bytes(buf.getvalue(), "ad.png")
