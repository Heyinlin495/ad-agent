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
    from app.image.poster import _section_heights

    sec = _section_heights(H, True, True, True)
    print("分区高度:", {k: f"{v}px({v/H*100:.1f}%)" for k, v in sec.items()})

    y = sec["hero"]
    grid_runs = _text_line_heights(img, y, y + sec["grid"])
    print(f"网格区文字行高: {grid_runs}")
    y += sec["grid"]
    det_runs = _text_line_heights(img, y, y + sec["details"])
    print(f"细节区文字行高: {det_runs}")
    y += sec["details"]
    ft_runs = _text_line_heights(img, y, H)
    print(f"底栏文字行高: {ft_runs}")

    floor = H * 0.018
    print(f"可读性地板 = {floor:.1f}px")
    body = [h for h in (grid_runs + det_runs) if h > 3]
    worst = min(body) if body else 0
    print(f"正文最小行高 = {worst}px  ->  {'PASS' if worst >= floor else 'FAIL'}")

    out = Path(__file__).resolve().parent.parent / "poster_probe.png"
    img.save(out)
    print(f"已保存 {out}")
    return 0 if worst >= floor else 1


if __name__ == "__main__":
    raise SystemExit(main())
