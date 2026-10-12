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
from loguru import logger

from app.image.fonts import load_font
from app.image.detail_tiles import DETAIL_ASPECT
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
# 🚨 底栏场景词 0.022（1080 下 23px）在手机端偏小（实测缩到 360px 宽的屏幕上只有
# 7.7px 视觉高度）。电商主图有相当比例是在手机上滑过第一眼的，底栏是"使用场景"
# 信息位，读不清等于没写。提到 0.026（28px），并同步放宽每格可用宽度（见
# `_draw_scene_footer`），避免被自动缩字又压回去。
_FS_FOOTER = 0.026         # 底部场景条 · 场景词
_FS_EYEBROW = 0.022        # 主视觉 · 眉题 / 品牌行
_FS_HERO_TITLE = 0.130     # 主视觉 · 主标题基准（自适应放大起点）
_FS_HERO_TITLE_MAX = 0.170  # 主视觉 · 主标题放大上限
# 🚨 副标题 0.050（54px）是"字号还没修好时"拍的值：当时所有文字实际只有 10px，
# 系数必须开大才勉强可见。字体修好后一量，副标题会折成 2 行、与 2 行大标题一起
# 吃掉约 81% 的主视觉区——商品被文案压住（商品才是广告主角）。收敛到 0.036
# （1080 下约 39px，仍是画布高的 3.6%），既清楚又留得住商品。
_FS_HERO_SUB = 0.036       # 主视觉 · 副标题

# 字号地板（绝对像素）：任何情况下不低于此值，避免极小画布下彻底不可读
_MIN_FONT_PX = 14

# 🚨 细节四格的目标画幅（宽, 高）。**必须与出图端一致**（`generator.SIZE_PRESETS["detail"]`）。
# 历史事故：旧版固定格子宽 239 后再把 1:1 出图居中裁切 → 图块实际是 239×111
# （2.15:1，纵向裁掉 53.6%），标题点名的部件（接口/配件）往往正好在切掉的部分里，
# 四格于是都只剩"整机中段"，看起来像同一张图。
DETAIL_TILE_ASPECT = DETAIL_ASPECT

# 细节四格横向间距（占画布宽）。旧值 0.018（19px）四张卡片挤在一起像一条连拍带，
# 电商细节图应该一眼看出是"四张独立的卡"。
_DETAIL_GAP = 0.026

# ---------------------------------------------------------------- 主视觉分区（文字 / 商品）
# 🚨 线上实测缺陷：主标题「Clear Calls No Charging」与耳机主体**叠在一起**，
# 层次不清。原因有三层：
#   ① 文案原本横跨 `w - 2*pad`（0.89 倍画布宽），必然压到居中/偏左的商品；
#   ② 场景提示词的 `text_area` 对非穿戴品类会退化成 `"top"`，通道 A 于是把商品
#      放在**左侧**（见 nodes._scene_prompt），而海报文字固定在**左下角** → 必重叠；
#   ③ 蒙版是"自下而上"的一整条，没有"文字侧"概念，商品那一侧也被压暗，
#      文字与商品在任何一条水平线上都可能撞色。
# 现在改为：宣传文案**恒占一侧的固定列**（默认左列 0.60 画布宽），场景图向
# **非文字侧**偏移让位，蒙版改为"文字侧 + 底部"的二维渐变。
_HERO_TEXT_COL = 0.60      # 主视觉文案列宽上限（占画布宽）
_HERO_TEXT_MIN_COL = 0.30  # 文案列宽下限（再窄就没法排大字号了）
_HERO_SIDE_BIAS = 0.97     # 长宽比不匹配时，场景图向非文字侧偏移的比例


def _hero_scrim(w: int, h: int, text_side: str, strength: int = 238) -> Image.Image:
    """主视觉蒙版：**文字侧 + 底部**二维渐变（返回 L 通道 alpha）。

    旧版只有"自下而上"的一维渐变，整幅等宽——文字在左、商品在右时，商品那一侧
    同样被压暗，既削弱了商品，也没给文字让出对比度优势（实测"背景对比不够理想"）。
    现在横向再乘一个"文字侧权重"：文字侧最实、对侧保留 30% 底噪，形成
    "左暗右亮"的分区感，商品从暗区里"亮出来"。
    """
    sw, sh = 64, 96
    grad = Image.new("L", (sw, sh), 0)
    px = grad.load()
    for j in range(sh):
        t = j / (sh - 1)
        vert = t ** 1.25
        for i in range(sw):
            u = i / (sw - 1)
            side_f = (1.0 - u) if text_side == "left" else u
            side_f = 0.30 + 0.70 * side_f
            px[i, j] = int(strength * vert * side_f)
    return grad.resize((w, h), Image.BILINEAR)


def _photo_subject_edge(img: Image.Image, text_side: str) -> float | None:
    """估计商品**朝向文字侧的那条边**的归一化 x（文字在左 → 商品左缘）。

    做法：对局部对比度（原图 − 强模糊）求列能量，取"从文字侧起累积到总能量 10%"
    的位置。用**累积量**而不是单列峰值，可以避开细高的干扰（线材、高光边缘往往
    峰值很高但只占极少列）；能量过于分散（没有明确主体）时返回 None，表示不加约束。
    """
    try:
        import numpy as np
        from PIL import ImageFilter

        g = img.convert("L").resize((160, 160), Image.LANCZOS)
        a = np.asarray(g, dtype=np.float32)
        b = np.asarray(g.filter(ImageFilter.GaussianBlur(5)), dtype=np.float32)
        col = np.abs(a - b).sum(axis=0)
        tot = float(col.sum())
        if tot <= 1e-6 or float(col.max()) < tot / 160.0 * 3:
            return None
        cum = np.cumsum(col) / tot
        if text_side == "left":
            return float(int(np.argmax(cum >= 0.10))) / 159.0
        return 1.0 - float(int(np.argmax(cum[::-1] >= 0.10))) / 159.0
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"商品边缘估计不可用，文案列宽用默认值: {exc}")
        return None


def _photo_subject_side(img: Image.Image) -> float | None:
    """估计画面里**商品**在横向的位置（归一化 0~1）；不可靠时返回 None。

    用**局部对比度**（原图 − 强模糊）作为显著度代理：影棚图里商品带大量高频细节，
    背景是平滑渐变/纯色，于是能量集中在商品上。单纯用"亮度偏离中位数"会被打光的
    光斑带偏（实测：聚光灯中心在画面正中，会把质心硬拽到 0.5）。
    """
    try:
        import numpy as np
        from PIL import ImageFilter

        g = img.convert("L").resize((128, 128), Image.LANCZOS)
        a = np.asarray(g, dtype=np.float32)
        b = np.asarray(g.filter(ImageFilter.GaussianBlur(6)), dtype=np.float32)
        col = np.abs(a - b).sum(axis=0)
        tot = float(col.sum())
        if tot <= 1e-6:
            return None
        # 显著度过于分散（没有"主体"）→ 不可靠，交给默认文字侧
        if float(col.max()) < tot * 0.03:
            return None
        return float((col * np.arange(128)).sum() / tot) / 127.0
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"商品侧估计不可用，沿用默认文字侧: {exc}")
        return None


def _choose_text_side(design: dict[str, Any], hero_image_bytes: bytes) -> str:
    """决定主视觉文案放哪一侧。

    默认跟随策划节点的 `text_area`（通道 A 的场景提示词按它布置商品，通常能让
    "商品偏右 + 左侧留白"成立）。但**模型并不总是听话**：实测出图里商品跑到了画面
    偏左，若还按默认把文字放左侧，就正好压上去。这里在渲染前估计商品的横向位置，
    商品明显压在文字侧时**把文字换到另一侧**——宁可文字换边，也不压商品。
    """
    side = str(design.get("text_area") or "left").strip().lower()
    if side not in {"left", "right"}:
        side = "left"
    try:
        img = Image.open(io.BytesIO(hero_image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001
        return side
    cx = _photo_subject_side(img)
    if cx is None:
        return side
    if side == "left" and cx < 0.44:
        return "right"
    if side == "right" and cx > 0.56:
        return "left"
    return side


def _fit_contain_biased(
    img: Image.Image,
    size: tuple[int, int],
    text_side: str,
    dark: tuple[int, int, int] = (28, 30, 38),
    bias: float = _HERO_SIDE_BIAS,
) -> Image.Image:
    """等比放入画布，文字侧补一块**品牌深色渐变面板**，并把画面推向非文字侧。

    主视觉区是"宽条"（1:1 画布下 1080×498 ≈ 2.17:1），而通道 A 出图多为 1:1/3:2：
    直接 cover 会纵向切掉一半以上，所以走 contain。但 contain 居中放置时商品正好
    落在画面正中，左侧文字必然压上去——这里把画面**靠非文字侧贴边**，
    空出的那一侧作为文字区。

    🚨 补边**不能用原图放大模糊**：模糊延展会保留画面的高频信息，与缩放后的清晰
    画面之间出现一条"贴图没贴满"的竖直接缝（实测渲染出来非常显眼）。改用品牌深色
    渐变面板后，接缝变成"照片面板的羽化边"，是设计意图而不是瑕疵，而且天然给文字
    让出对比度。
    """
    tw, th = size
    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        return Image.new("RGBA", size, dark + (255,))
    scale = min(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale + 0.5)), max(1, int(sh * scale + 0.5))
    fitted = img.resize((nw, nh), Image.LANCZOS)

    # 品牌深色面板：纵向做极轻的明暗变化，避免死板纯色
    bg = Image.new("RGBA", (tw, th), dark + (255,))
    grad = Image.new("L", (1, 64), 0)
    gp = grad.load()
    for j in range(64):
        gp[0, j] = int(38 * (1 - j / 63))          # 顶部略亮 38 级
    tint = Image.new("RGBA", (tw, th), (255, 255, 255, 0))
    tint.putalpha(grad.resize((tw, th)))
    bg = Image.alpha_composite(bg, tint)

    slack_x = max(0, tw - nw)
    frac = bias if text_side == "left" else (1.0 - bias)
    x = int(slack_x * frac)
    # 朝空白侧羽化：让照片"溶"进深色面板，而不是贴一条硬边上去
    feather = max(8, int(nw * 0.22))
    if feather < nw:
        m = Image.new("L", (nw, 1), 255)
        mp = m.load()
        for i in range(feather):
            v = int(255 * (i / feather))
            mp[i if text_side == "left" else nw - 1 - i, 0] = v
        fitted.putalpha(m.resize((nw, nh)))
    bg.alpha_composite(fitted, (x, max(0, (th - nh) // 2)))
    return bg


def _fit_hero(
    image_bytes: bytes,
    size: tuple[int, int],
    text_side: str,
    design: dict[str, Any] | None = None,
    dark: tuple[int, int, int] = (28, 30, 38),
) -> Image.Image:
    """主视觉场景图 → 主视觉区尺寸，**以"文字侧留白"为优先目标**。

    长宽比接近时用 `composer._smart_fit_cover`（扫描所有合法裁剪窗口，选
    "含住主体 + 文字侧余量最大"的那个）；差异大时用 `_fit_contain_biased`
    （不裁画面，改把画面推向非文字侧、另一侧补品牌深色面板）。
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    sw, sh = img.size
    tw, th = size
    if sw > 0 and sh > 0:
        src_r = sw / sh
        dst_r = tw / th
        if abs(src_r - dst_r) / max(src_r, dst_r) > 0.06:
            return _fit_contain_biased(img, size, text_side, dark)
        return C._smart_fit_cover(img, size, text_side, design)
    return Image.new("RGBA", size, dark + (255,))


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


def _fit_line(
    text: str, font: Any, max_w: int, max_lines: int, balanced: bool = False
) -> list[str]:
    """把 `text` 换行并截断到 `max_lines` 行，**超出的部分丢掉**。

    🚨 旧版用切片 `[...][:max_lines]` 截断，但渲染循环却按 `font.size` 逐行推 y，
    于是"被截掉的行"的位置仍是空的，视觉上像是漏了一行。这里统一在换行阶段收口。

    `balanced=True` 时用**平衡换行**（各行宽度尽量接近）。主视觉大标题必须用它：
    贪心换行会把 "CLEAR CALLS. ZERO CHARGING." 排成 `CLEAR CALLS. / ZERO / CHARGING.`
    （中间一行只剩一个孤零零的 ZERO），平衡换行给出 `CLEAR / CALLS. ZERO / CHARGING.`。
    """
    lines = (C._wrap_text_balanced(text, font, max_w) if balanced
             else C._wrap_text(text, font, max_w))
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
    底栏压到全图 5.5%（1080 下仅 59px），连"图标 + 场景词"都排不开；后续调为
    hero .50 / grid .26 / details .185 / footer .075。

    🚨 2026-10-12 再调一次：细节带从 .185 提到 .235，从主视觉匀出 28px。
    原因是四格画幅改成与出图一致的 3:2（`DETAIL_TILE_ASPECT`）后，格子需要
    `cell_w / 1.5` 的高度；沿用 .185 会把格子从 2.15:1 挤成更扁的比例，
    等于把"细节特写"又压回"整机中段"。主视觉减 31px 只是少一点留白。
    另外底栏单独 clamp 一个**最低高度**，防止素材降级时被进一步压扁。
    """
    weights = {"hero": 0.485}
    if has_grid:
        weights["grid"] = 0.255
    if has_details:
        weights["details"] = 0.235
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
    text_side: str = "left",
) -> None:
    """把 AI 场景图贴进主视觉区，并叠加"蒙版 + 文案"。

    🚨 **文字与商品分区**：文案恒定占**一侧的固定列**（`_HERO_TEXT_COL`，默认左列
    0.60 画布宽），不再横跨 0.89 画布宽。历史缺陷：标题横跨整幅 → 与耳机主体重叠，
    "层次不清晰"（线上实测）。列的宽度是硬约束：标题再长也只在本列内换行/缩字。
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

    # 蒙版：**文字侧 + 底部**二维渐变（见 `_hero_scrim`）。旧版是"自下而上"的一整条，
    # 商品那一侧同样被压暗，既没给文字让出对比度优势，也削弱了商品。
    side = text_side if text_side in {"left", "right"} else "left"
    overlay = Image.new("RGBA", (w, h), (6, 8, 14, 255))
    overlay.putalpha(_hero_scrim(w, h, side))
    poster.alpha_composite(overlay, (x0, y0))

    draw = ImageDraw.Draw(poster)
    tc = (246, 246, 250)
    pad = int(w * 0.055)
    # 文案列：宽 = min(画布宽 × `_HERO_TEXT_COL`, **商品朝文字侧的边缘** − 边距)。
    # 只按固定比例定列宽是不够的：实测出图里商品落在画布中部 0.47~0.80，
    # 0.60 倍宽的文案列右缘（0.65）仍会压到商品左缘。这里用局部对比度估计商品边缘，
    # 把列宽收到"刚好不碰商品"，宁可列窄一点、标题改成纵向堆叠，也不压商品。
    col_default = int(w * _HERO_TEXT_COL)
    col_w = col_default
    edge = _photo_subject_edge(hero, side)
    if edge is not None:
        limit = int(w * edge) - pad - int(w * 0.02)
        col_w = max(int(w * _HERO_TEXT_MIN_COL), min(col_default, limit))
    tx = x0 + pad if side == "left" else x1 - pad - col_w
    max_w = col_w
    align_right = side == "right"
    # 字号锚定**画布总高**（poster.size[1]），而非主视觉区高度——这样主视觉区
    # 高度被其它分区挤压时，标题不会跟着缩水（历史事故见文件顶部说明）。
    canvas_h = poster.size[1]

    def _put(ln: str, font: Any, yy: int, fill: tuple[int, int, int]) -> None:
        """按文字侧对齐方式落字（右侧文字右对齐，避免"方块字悬在中间"）。"""
        if align_right:
            draw.text((tx + max_w - int(font.getlength(ln)), yy), ln, font=font, fill=fill)
        else:
            draw.text((tx, yy), ln, font=font, fill=fill)

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
            f = load_font(max(_MIN_FONT_PX, int(px)), bold=bold, text=text)
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
            f = load_font(max(_MIN_FONT_PX, int(px)), bold=bold, text=text)
            lines = C._wrap_text_balanced(text, f, max_w)
            if len(lines) <= max_lines and all(f.getlength(ln) <= max_w for ln in lines):
                return f
            px *= 0.94
        return load_font(max(_MIN_FONT_PX, int(px)), bold=bold, text=text)

    # 自底向上堆叠（贴主视觉区底部）：副标题 → 主标题 → 眉题
    # 行高一律用字体真实度量 `_line_h`，否则大字号下相邻行会纵向重叠。
    #
    # 🚨 纵向容量是**硬约束**：hero 区高度固定，而"眉题 + 品牌行 + 标题 + 副标题"
    # 的总高度可能超出它（实测：3 行超长标题 + 2 行超长副标题 → 文字溢出到网格区、
    # 顶部露出黑边）。因此这里先算出各块的"高度预算"，超预算就按**优先级**压缩：
    # 副标题（1 行）→ 标题（2 行）→ 眉题（字号下限）。绝不能靠"整体平移"补救。
    y = y1 - pad

    label_font = load_font(_fs(canvas_h, _FS_EYEBROW), bold=True, text=eyebrow)
    brand_font = load_font(_fs(canvas_h, _FS_EYEBROW), bold=True, text=brand)
    # 顶部预留：品牌行（若存在）占其行高 + 顶部边距
    top_reserve = y0 + int(h * 0.045) + (_line_h(brand_font) if brand else 0)
    # 眉题占位
    eyebrow_h = int(_line_h(label_font) * 1.6) if eyebrow else 0
    # 可供标题 + 副标题使用的总高度
    text_budget = max(0, (y1 - pad) - top_reserve - eyebrow_h)
    # 🚨 上限：文案块最多占主视觉区高的 0.62。商品是广告主角，文案再大也不能把它
    # 盖住——实测（字体修好后）2 行大标题 + 2 行副标题会吃掉 hero 的 81%。
    # 注意要**扣除眉题占位**：0.62 是"整块文案（含眉题）"的上限，而 `text_budget`
    # 只统计标题+副标题。文案列被商品边缘压窄后标题会涨到 4 行、把预算吃满，
    # 不再扣眉题就会实打实超到 70%（实测 0.699 → 用例红灯）。
    text_budget = min(text_budget, max(0, int(h * 0.62) - eyebrow_h))

    if subheadline:
        base_s = _fs(canvas_h, _FS_HERO_SUB)
        # 🚨 "副标题降成 1 行"必须靠**缩字号**实现，不能靠 `_fit_line(..., 1)` 截断：
        # `_fit_line` 是"超出部分丢掉"，实测把副标题拦腰砍成
        # "Wired headphones built for work,"（后半句消失）。文案列变窄后这条路径更容易触发。
        sub_font = _shrink_to_fit(subheadline, base_s, max_w, 2, balanced=True)
        sub_lines = _fit_line(subheadline, sub_font, max_w, 2, balanced=True)
        # 副标题是**次要信息**：若与标题相加超出纵向预算，先牺牲它（缩到 1 行）。
        # 阈值取 0.32（原 0.45 太宽松）：副标题折成 2 行时往往正好把标题挤到可读下限。
        if len(sub_lines) * _line_h(sub_font) > text_budget * 0.32:
            sub_font = _shrink_to_fit(subheadline, base_s, max_w, 1, balanced=True)
            sub_lines = _fit_line(subheadline, sub_font, max_w, 1, balanced=True)

    if headline:
        sub_h = len(sub_lines) * _line_h(sub_font) if subheadline else 0
        avail_for_head = text_budget - sub_h
        # 🚨 行数既不是越多越好、也不是越少越好：列宽固定时"字大"与"行少"互相冲突。
        # 实测同一句文案：0.60 列宽下 3 行能到 112px、4 行反而只有 90px；而列被商品
        # 边缘压窄到 0.40 时，2/3 行会被最长单词卡到 52~66px，**4 行堆叠反而能到 76px**。
        # 所以逐个行数预算求解，再按"有效字号（行数多给一点折扣）"挑最优解。
        best: tuple[float, Any, list[str]] | None = None
        for budget_lines in (2, 3, 4):
            f = _fit_font(
                headline,
                _fs(canvas_h, _FS_HERO_TITLE),
                True,
                budget_lines,
                max_px=_fs(canvas_h, _FS_HERO_TITLE_MAX),
            )
            # `_fit_font` 内部用"平衡换行"，与 `_fit_line` 不同源，行数可能差一行；
            # 再套 `_shrink_to_fit` 兜底，保证**任何情况下都不截断**（缺字比字小更糟）。
            f = _shrink_to_fit(headline, f.size, max_w, budget_lines, bold=True, balanced=True)
            ls = _fit_line(headline, f, max_w, budget_lines, balanced=True)
            for _ in range(20):
                if len(ls) * _line_h(f) <= avail_for_head or f.size <= _MIN_FONT_PX:
                    break
                f = _shrink_to_fit(
                    headline, int(f.size * 0.92), max_w, budget_lines, bold=True, balanced=True
                )
                ls = _fit_line(headline, f, max_w, budget_lines, balanced=True)
            score = f.size * (1.0 - 0.05 * (budget_lines - 2))
            if best is None or score > best[0]:
                best = (score, f, ls)
        _, font_h, h_lines = best

    # 自底向上落字：副标题贴底，主标题压在其上（视觉上仍是"标题在上、副标题在下"）
    if subheadline and sub_lines:
        y -= len(sub_lines) * _line_h(sub_font)
        yy = y
        for ln in sub_lines:
            _put(ln, sub_font, yy, _muted(tc))
            yy += _line_h(sub_font)

    if headline and h_lines:
        y -= len(h_lines) * _line_h(font_h)
        yy = y
        for ln in h_lines:
            _put(ln, font_h, yy, tc)
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
        e_txt = eyebrow.upper()
        _put(e_txt, label_font, y, e_col)
        # 眉题的短横线（参考海报的分隔线），画在**朝商品那一侧**。
        # 🚨 旧版用**白色**、`canvas_h*0.005`（1080 下 5px）粗、占宽 14%，在深色底上
        # 是一块很扎眼的白条，像没画完的占位符。现在改用**强调色**、约 2px、占宽 10%。
        _, accent = _palette(design)
        lw = int(label_font.getlength(e_txt))
        seg = int(w * 0.10)
        off = int(label_font.size * 0.7)
        bar_h = max(1, int(canvas_h * 0.0022))
        ly = y + int((_line_h(label_font) - bar_h) / 2)
        if align_right:
            ex = tx + max_w - lw
            lx0, lx1 = max(tx, ex - off - seg), ex - off
        else:
            lx0 = tx + lw + off
            lx1 = min(tx + max_w, lx0 + seg)
        draw.rectangle([lx0, ly, lx1, ly + bar_h], fill=accent)

    # 记录文案块占主视觉区的比例（回归用）：字号一旦重新失控、开始压住商品，
    # 这里会最先露头。设计约束见上方 `text_budget` 的 0.62 上限。
    design["_hero_text_h"] = int((y1 - pad) - y)
    design["_hero_text_ratio"] = round(design["_hero_text_h"] / max(1, h), 3)
    # 文字侧与文案列宽（回归用）：锁定"文字占一侧、另一侧留给商品"这条版式契约。
    design["_hero_text_side"] = side
    design["_hero_text_col_w"] = int(max_w)

    # 顶部左侧品牌行。
    # 🚨 不要再回退到 `_tag`：`_tag`（category_tag）已作为 `eyebrow` 参数传给本函数、
    # 渲染在文案区上方，若此处再用同一个字符串，同一张图会出现两处 "AUDIO"
    # （品类标签语义上是"右上角标签"，不该重复出现在标题区顶部）。
    # 品牌为空时就**不画**这一行，把空间让给文案。
    if brand:
        font_b = load_font(_fs(canvas_h, _FS_EYEBROW), bold=True, text=brand)
        b_y0 = y0 + int(h * 0.045)
        # 标签行按背景亮度选字色，避免"白字压浅底"看不见
        bl = C._region_luminance(poster, (int(tx), int(b_y0), int(tx + max_w), int(b_y0 + font_b.size)))
        b_col = (246, 246, 250) if bl < 140 else (28, 30, 36)
        _put(brand[:30], font_b, b_y0, b_col)


def _shrink_to_fit(
    text: str,
    start_px: int,
    max_w: int,
    max_lines: int,
    bold: bool = False,
    min_px: int = _MIN_FONT_PX,
    balanced: bool = False,
) -> Any:
    """从 `start_px` 起逐级缩小字号，直到文本能装进 `max_lines` 行且每行不溢出。

    用于网格/细节带这类**单行本应放下、但个别长词会撑破栏宽**的场景：
    旧版直接按原字号换行再截断，长标题会被拦腰砍断（实测"A"+"Accessories"残行）。
    换行方式必须与真正渲染时一致（`balanced`），否则"缩到放得下"的判定会和渲染结果对不上。
    """
    px = float(start_px)
    for _ in range(24):
        f = load_font(max(min_px, int(round(px))), bold=bold, text=text)
        lines = (C._wrap_text_balanced(text, f, max_w) if balanced
                 else C._wrap_text(text, f, max_w))
        if len(lines) <= max_lines and all(f.getlength(ln) <= max_w for ln in lines):
            return f
        px *= 0.94
    return load_font(max(min_px, int(round(px))), bold=bold, text=text)


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
            f = load_font(px, bold=bd, text=seg)
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
    fonts = [load_font(max(_MIN_FONT_PX, int(round(b * k))), bold=bd, text=seg)
             for seg, b, bd in zip(segments, start_px_list, bolds)]
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
    # 说明文字与标题**左对齐同一起点**（旧版说明额外缩进，四格的"文字起点"不一致）
    title_w = cell_w - max_icon_r
    fonts, _ = _fit_block(
        [longest_t, longest_d],
        [_fs(canvas_h, _FS_GRID_TITLE), _fs(canvas_h, _FS_GRID_DESC)],
        title_w,
        [2, 2],
        avail_h,
        [True, False],
    )
    font_t, font_d = fonts[0], fonts[1]

    # 🚨 **严格两列两行**：四格共用一组基线。
    # 旧版每格各自"在剩余空间里垂直居中"——标题 2 行的格子块高、1 行的块矮，
    # 于是两格文字的重心错开（实测"第一项两行、第二项一行，视觉重心不平衡"）。
    # 现在先把四格的换行结果都算出来，取**统一的行槽数**（槽位不足的格子留空），
    # 图标 / 标题首行 / 说明首行的 y 在四格中完全一致。
    cells: list[tuple[list[str], list[str]]] = []
    for title, desc in items:
        cells.append((
            _fit_line(title, font_t, title_w, 2),
            _fit_line(desc, font_d, title_w, 2) if desc else [],
        ))
    t_slots = max((len(t) for t, _ in cells), default=0)
    d_slots = max((len(d) for _, d in cells), default=0)

    line_t, line_d = _line_h(font_t), _line_h(font_d)
    gap_ic = int(cell_h * 0.04)      # 图标 → 标题
    gap_td = int(cell_h * 0.03)      # 标题 → 说明
    block_h = 2 * max_icon_r + gap_ic + t_slots * line_t
    if d_slots:
        block_h += gap_td + d_slots * line_d
    top_off = max(0, (cell_h - block_h) // 2)
    icon_cy_off = top_off + max_icon_r
    title_y_off = top_off + 2 * max_icon_r + gap_ic
    desc_y_off = title_y_off + t_slots * line_t + gap_td

    for i, (t_lines, d_lines) in enumerate(cells):
        r, c = divmod(i, cols)
        cx = x0 + pad_x + c * cell_w
        cy = y0 + pad_y + r * cell_h
        icon_cx = cx + max_icon_r
        icon_cy = cy + icon_cy_off
        draw.ellipse(
            [icon_cx - max_icon_r, icon_cy - max_icon_r,
             icon_cx + max_icon_r, icon_cy + max_icon_r],
            outline=accent, width=max(2, int(max_icon_r * 0.22)),
        )
        _draw_check(draw, icon_cx, icon_cy, max_icon_r, accent)

        ty = cy + title_y_off
        for ln in t_lines:
            draw.text((cx, ty), ln, font=font_t, fill=fg)
            ty += line_t
        ty = cy + desc_y_off
        for ln in d_lines:
            draw.text((cx, ty), ln, font=font_d, fill=_muted(fg))
            ty += line_d


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

    🚨 画幅必须与出图端一致（`DETAIL_TILE_ASPECT` = 3:2）。旧版先按可用宽度定
    `cell_w`，再把 1:1 出图 `cover` 裁进去 → 图块 239×111（2.15:1，纵向裁掉
    53.6%）。"接口 / 配件"这类边缘部件正好在切掉的部分里，四格于是都只剩整机中段。
    现在先算高、再按高反推宽（不够宽就整条居中），出图与格位同比例 → 几乎零裁切。
    """
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    if not detail_imgs:
        return
    n = min(4, len(detail_imgs))
    draw = ImageDraw.Draw(poster)
    fg = _text_on(dark)
    gap = int(w * _DETAIL_GAP)
    pad_x = int(w * 0.03)
    pad_y = int(h * 0.06)
    cell_w = (w - 2 * pad_x - gap * (n - 1)) // n
    # 标题带高度按**画布字号 + 真实行高**反推，而不是按分区高度的固定比例——
    # 否则字号调大后标题会顶到图片下沿（旧版 cap_h=h*0.22 时标题会压到图上）。
    cap_font_px = _fs(poster.size[1], _FS_DETAIL_CAP)
    cap_line = _line_h(load_font(cap_font_px, bold=True))
    cap_h = cap_line + int(h * 0.08)
    img_h_max = max(1, h - 2 * pad_y - cap_h)

    ratio = DETAIL_TILE_ASPECT[0] / DETAIL_TILE_ASPECT[1]
    img_h = int(round(cell_w / ratio))
    if img_h > img_h_max:            # 竖向不够：按高度反推宽度，整条居中
        img_h = img_h_max
        cell_w = int(round(img_h * ratio))
    strip_w = cell_w * n + gap * (n - 1)
    x_start = x0 + max(pad_x, (w - strip_w) // 2)

    for i in range(n):
        cx = x_start + i * (cell_w + gap)
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
    # 🚨 图标放在每格**左端**、文字紧随其后，与卖点网格的"图标 + 文字"语汇一致，
    # 四格的文字起点也因此对齐。旧版把图标摆在格中心偏左、文字从格中心起排，
    # 且字号偏小（0.022 → 23px），手机上读不清。
    icon_r = max(6, int(h * 0.20))
    pad = int(seg * 0.08)
    for i, sc in enumerate(scenes[:4]):
        x_seg = int(seg * i)
        icon_cx = x_seg + pad + icon_r
        icon_cy = y0 + h // 2
        tx = icon_cx + icon_r + int(seg * 0.05)
        avail_w = max(24, int(seg) - (tx - x_seg) - pad)
        font_s = _shrink_to_fit(str(sc), _fs(poster.size[1], _FS_FOOTER), avail_w, 2)
        # 小圆点图标（+ 与网格同款对勾，保持图标族一致）
        draw.ellipse([icon_cx - icon_r, icon_cy - icon_r, icon_cx + icon_r, icon_cy + icon_r],
                     outline=accent, width=max(2, int(icon_r * 0.3)))
        _draw_check(draw, icon_cx, icon_cy, icon_r, accent)
        lines = _fit_line(str(sc), font_s, avail_w, 2)
        lh = _line_h(font_s)
        ty = icon_cy - (len(lines) * lh) // 2
        for ln in lines:
            draw.text((tx, ty), ln, font=font_s, fill=fg)
            ty += lh


def _draw_dividers(
    poster: Image.Image,
    ys: list[int],
    dark: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> None:
    """在分区边界画一条极细的亮色分隔线。

    hero / 卖点网格 / 细节带 / 底栏用的是**同一个最深底色**，不画分隔线时四段会
    糊成一整片（实测观感"这张图没有分段"）。线宽按画布高的 0.15% 取（1080 下约
    1.6px），颜色＝底色混一点强调色再提亮，保证"看得见但不抢戏"。
    """
    w, h = poster.size
    if not ys or h <= 0:
        return
    draw = ImageDraw.Draw(poster)
    px = max(1, int(h * 0.0015))
    col = tuple(min(255, int((d * 0.72 + a * 0.28) * 1.35)) for d, a in zip(dark, accent))
    for y in ys:
        yy = int(y)
        if 0 <= yy < h:
            draw.rectangle([0, yy, w, yy + px], fill=col)


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

    # 文字侧：策划节点写入的 `text_area`（已归一化为 left/right）为默认；
    # 再由 `_choose_text_side` 按**画面里商品的实际位置**校正（模型没听话时换边）。
    text_side = _choose_text_side(design, hero_image_bytes)

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

    # 🚨 主视觉必须**按主视觉区自己的长宽比**（1080×498 ≈ 2.17:1）来裁剪，而不是先按
    # 整张画布（1:1）cover、再在 `_draw_hero_section` 里二次 cover——两步都居中，
    # 商品必然落在标题底下。`_fit_hero` 同时负责"文字侧留白优先 + 商品让位"。
    hero_img = _fit_hero(hero_image_bytes, (W, sec["hero"]), text_side, design, dark)

    poster = Image.new("RGBA", (W, H), dark + (255,))
    y = 0
    hero_box = (0, 0, W, sec["hero"])
    _draw_hero_section(
        poster, hero_img, design, headline, subheadline,
        (design.get("_brand") or "").strip(), (design.get("_tag") or "").strip(),
        hero_box, text_side,
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

    # 分区边界分隔线（四个分区共用同一个最深底色，不加线会糊成一片）
    bounds: list[int] = []
    yb = sec["hero"]
    for present, key in ((has_grid, "grid"), (has_details, "details"), (has_footer, "footer")):
        if present:
            bounds.append(yb)
            yb += sec[key]
    _draw_dividers(poster, bounds, dark, accent)

    return poster.convert("RGB")
