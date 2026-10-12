"""海报字号/版式冒烟探针（本地开发用，不依赖 LLM 与外部服务）。

用法：
    cd backend && python tools/poster_probe.py

它用合成素材直接渲染一张 1080x1080 海报，然后**逐行扫描**各分区的最小文字
行高，输出一份可读性报告。用于验证字号改动是否生效、是否出现溢出。
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from app.image.poster import compose_poster  # noqa: E402


def _hero_bytes() -> bytes:
    img = Image.new("RGB", (1600, 1600), (232, 236, 230))
    d = ImageDraw.Draw(img)
    d.ellipse([520, 380, 1080, 940], fill=(38, 40, 44))
    d.ellipse([620, 480, 980, 840], fill=(58, 60, 66))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _detail_bytes(tone: int) -> bytes:
    img = Image.new("RGB", (900, 900), (238, 238, 240))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([180, 180, 720, 720], radius=60, fill=(tone, tone, tone + 4))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _text_line_heights(img: Image.Image, y0: int, y1: int, thresh: int = 150) -> list[int]:
    """扫描 [y0,y1) 行区间内的亮像素行，返回各文字行的连续高度。"""
    gray = img.convert("L")
    px = gray.load()
    w = gray.size[0]
    runs: list[int] = []
    cur = 0
    for y in range(y0, min(y1, gray.size[1])):
        has = any(px[x, y] > thresh for x in range(0, w, 2))
        if has:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return runs


def main() -> int:
    # 🚨 字体门禁（2026-10-12）：`load_font()` 找不到字体时会静默退化为固定 10px
    # 的默认字体，此时渲染出来的"行高"看着正常，但字号验证毫无意义——是**假绿**。
    # Windows 有 msyh.ttc 兜底，线上 slim 容器没有，所以必须显式挡住。
    from app.image.fonts import describe_font_status, font_status

    fst = font_status()
    print(describe_font_status())
    if not fst["ok"]:
        print("FAIL: 环境缺字体，字号验证不可信（请先装 fonts-dejavu-core / fonts-noto-cjk）")
        return 2

    design = {
        "palette": ["#1C1E26", "#B08A54"],
        "_brand": "",              # 故意留空：验证回退到 _tag
        "_tag": "AUDIO",
        "_tagline": "",
    }
    hero = _hero_bytes()
    details = [_detail_bytes(60 + i * 25) for i in range(4)]
    bullets = [
        "Lightweight comfort|Feel the ease",
        "Zero lag wired connection|Instant response",
        "Soft PU leather cushions|Gentle on skin",
        "Adjustable and foldable fit|Fits every head",
    ]
    captions = ["Textured Surface", "Precision Build", "Reinforced Hinge", "Included Accessories"]
    scenes = ["Office work", "Online learning", "Gaming", "Daily commuting"]

    img = compose_poster(
        hero,
        design,
        "All-Day Comfort Only $21",
        "Wired headphones built for work, study, and play",
        "Shop Now",
        "1:1",
        bullets=bullets,
        detail_image_bytes=details,
        detail_captions=captions,
        scenes=scenes,
        tagline="AUDIO",
    )
    W, H = img.size
    print(f"画布 {W}x{H}")

    # 分区高度
    from app.image.poster import (
        _FS_DETAIL_CAP,
        _FS_FOOTER,
        _FS_GRID_DESC,
        _FS_GRID_TITLE,
        _fs,
        _section_heights,
    )

    sec = _section_heights(H, True, True, True)
    print("分区高度:", {k: f"{v}px({v/H*100:.1f}%)" for k, v in sec.items()})

    # 主视觉「文字 / 商品分区」：文字必须落在固定的文字列内，另一侧留给商品。
    side = design.get("_hero_text_side")
    col_w = design.get("_hero_text_col_w")
    ratio_h = design.get("_hero_text_ratio")
    lim = int(W * 0.66)
    print(
        f"主视觉文字侧={side} 文案列宽={col_w}px 文案块占比={ratio_h}"
        f"  ->  {'PASS' if (col_w or 0) <= lim else 'FAIL'}"
    )
    ok_col = bool(side) and (col_w or 0) <= lim

    y = sec["hero"]
    grid_runs = _text_line_heights(img, y, y + sec["grid"])
    print(f"网格区文字行高: {grid_runs}")
    y += sec["grid"]
    det_runs = _text_line_heights(img, y, y + sec["details"])
    print(f"细节区文字行高: {det_runs}")
    y += sec["details"]
    ft_runs = _text_line_heights(img, y, H)
    print(f"底栏文字行高: {ft_runs}")

    # 🚨 判据分两层，避免"只对某一种字体的墨高成立"：
    #   ① 名义字号（我们真正控制的量）不得低于画布高 1.8%；
    #   ② 真实渲染的墨高不得相对名义字号塌陷——`ImageFont.load_default()`
    #      无视传入字号，名义 32px / 墨高 10px，会在这里被抓到。
    nominals = {
        "grid_title": _fs(H, _FS_GRID_TITLE),
        "grid_desc": _fs(H, _FS_GRID_DESC),
        "detail_cap": _fs(H, _FS_DETAIL_CAP),
        "footer": _fs(H, _FS_FOOTER),
    }
    floor = H * 0.018
    print(f"名义字号: {nominals}  地板 = {floor:.1f}px")
    body = [h for h in (grid_runs + det_runs) if h > 3]
    worst = min(body) if body else 0
    exp_ink = min(_fs(H, _FS_GRID_DESC), _fs(H, _FS_DETAIL_CAP)) * 0.55
    ok_nominal = min(nominals.values()) >= floor
    ok_ink = worst >= exp_ink
    print(f"正文最小墨高 = {worst}px（下限 {exp_ink:.1f}px）")
    print(
        f"名义字号地板 {'PASS' if ok_nominal else 'FAIL'}"
        f" / 墨高地板 {'PASS' if ok_ink else 'FAIL'}"
        f" / 文字分区 {'PASS' if ok_col else 'FAIL'}"
    )

    out = Path(__file__).resolve().parent.parent / "poster_probe.png"
    img.save(out)
    print(f"已保存 {out}")
    return 0 if (ok_nominal and ok_ink and ok_col) else 1


if __name__ == "__main__":
    raise SystemExit(main())
