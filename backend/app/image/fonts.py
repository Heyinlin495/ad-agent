"""字体加载与文本渲染（支持 CJK / 阿拉伯 RTL）。

搜索顺序：bundled assets/fonts -> 系统字体目录 -> 递归查找。

🚨 铁律（2026-10-12 事故）：`load_font()` 一旦找不到真实字体，旧实现会**静默**
返回 `ImageFont.load_default()`。该字体是**固定 10px 的默认字体，会完全忽略
传入的 size**，于是整条出图链路的文字（海报 hero 大标题、网格、CTA…）全部变成
10px，且没有任何日志——线上"字全都看不清"就是这么来的。
因此现在：① 兜底前**必打 ERROR 日志**；② 兜底前先尝试任意真实字体文件；
③ 提供 `font_status()/describe_font_status()` 供启动自检与健康检查。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from loguru import logger
from PIL import ImageFont

from app.core.config import settings

# ---------------------------------------------------------------- 字体候选
# 🚨 为什么按「文种 × 字重」拆成独立列表：
# 旧版把 Bold 与 Regular 混在一个列表里（`LATIN_FONT_CANDIDATES` 把
# `DejaVuSans-Bold.ttf` 排在 `DejaVuSans.ttf` 之前），并且 CJK 候选整体优先于
# 拉丁候选。结果：① `bold=False` 也会拿到粗体；② 只要镜像里装了任何中文字体，
# 英文标题就再也拿不到粗体（海报标题会明显变弱）。
CJK_REGULAR_CANDIDATES = [
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJKsc-Regular.otf",
    "NotoSansSC-Regular.otf",
    "NotoSansSC-Regular.ttf",
    "SourceHanSansSC-Regular.otf",
    "SourceHanSansCN-Regular.otf",
    "msyh.ttc",
    "msyh.ttf",
    "wqy-zenhei.ttc",
    "simhei.ttf",
    "simsun.ttc",
    "PingFang.ttc",
    "PingFang-SC-Regular.otf",
    "Arial Unicode.ttf",
]
CJK_BOLD_CANDIDATES = [
    "NotoSansCJK-Bold.ttc",
    "NotoSansSC-Bold.otf",
    "SourceHanSansSC-Bold.otf",
    "SourceHanSansCN-Bold.otf",
    "msyhbd.ttc",
    "msyhbd.ttf",
    "Arial Unicode.ttf",
]
LATIN_REGULAR_CANDIDATES = [
    "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
    "NotoSans-Regular.ttf",
    "Roboto-Regular.ttf",
    "Arial.ttf",
    "arial.ttf",
    "Helvetica.ttf",
    "Verdana.ttf",
    "segoeui.ttf",
]
LATIN_BOLD_CANDIDATES = [
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "NotoSans-Bold.ttf",
    "Roboto-Bold.ttf",
    "Arial Bold.ttf",
    "arialbd.ttf",
    "segoeuib.ttf",
]
ARABIC_REGULAR_CANDIDATES = [
    "NotoSansArabic-Regular.ttf",
    "NotoNaskhArabic-Regular.ttf",
    "Amiri-Regular.ttf",
]
ARABIC_BOLD_CANDIDATES = [
    "NotoSansArabic-Bold.ttf",
    "NotoNaskhArabic-Bold.ttf",
    "Amiri-Bold.ttf",
]

# 兼容旧引用（历史脚本/测试按这些名字取用）
CJK_FONT_CANDIDATES = CJK_BOLD_CANDIDATES + CJK_REGULAR_CANDIDATES
ARABIC_FONT_CANDIDATES = ARABIC_REGULAR_CANDIDATES + ARABIC_BOLD_CANDIDATES
LATIN_FONT_CANDIDATES = LATIN_BOLD_CANDIDATES + LATIN_REGULAR_CANDIDATES

# 衬线体（杂志版式大标题用）：优先高对比度编辑体，回退到系统通用衬线
SERIF_FONT_CANDIDATES = [
    "PlayfairDisplay-Bold.ttf",
    "PlayfairDisplay-Regular.ttf",
    "Didot.ttf",
    "Bodoni.ttf",
    "georgiab.ttf",
    "georgia.ttf",
    "cambriab.ttf",
    "cambria.ttc",
    "constanb.ttf",
    "constan.ttf",
    "timesbd.ttf",
    "times.ttf",
    "LiberationSerif-Bold.ttf",
    "DejaVuSerif-Bold.ttf",
    "DejaVuSerif.ttf",
]

# 判断文本是否含 CJK（决定优先用中文字体还是拉丁字体）
_CJK_RE = re.compile(
    r"[\u2e80-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)

_fallback_warned = False


def fonts_dir() -> Path:
    return Path(settings.assets_dir) / "fonts"


def _warn_fallback(where: str) -> None:
    """回退到 PIL 默认字体时告警（只告警一次，避免刷屏）。"""
    global _fallback_warned
    if _fallback_warned:
        return
    _fallback_warned = True
    logger.error(
        "🚨 未加载到任何字体文件，已回退 ImageFont.load_default()——"
        "该字体固定 10px 且**忽略传入字号**，出图文字会全部变成 10px。"
        f"（触发点：{where}）请确认镜像安装了字体（fonts-dejavu-core / fonts-noto-cjk），"
        f"或把字体文件放进 {fonts_dir()}。诊断：fonts.describe_font_status()"
    )


def _system_font_dirs() -> list[Path]:
    """返回当前操作系统可能存放字体的目录。"""
    dirs: list[Path] = []
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(Path(windir) / "Fonts")
    elif sys.platform == "darwin":
        dirs.extend([
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library/Fonts",
        ])
    else:
        dirs.extend([
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
        ])
    return [d for d in dirs if d.exists()]


def _find_font(candidates: list[str]) -> Path | None:
    """按优先级在 bundled 目录与系统字体目录中查找候选字体名。

    🚨 这里**不做**"任意字体兜底"。旧实现在最后一步会返回系统目录里第一个
    ttf/otf/ttc，导致 `_find_font(LATIN_*)` 可能返回一个中文字体——文种与
    字重的区分全部失效。兜底改由 `_find_any_font()` 单独承担。
    """
    # 1) 项目内置字体
    base = fonts_dir()
    for name in candidates:
        p = base / name
        if p.exists():
            return p

    # 2) 系统字体目录（直接命中）
    system_dirs = _system_font_dirs()
    for d in system_dirs:
        for name in candidates:
            p = d / name
            if p.exists():
                return p

    # 3) 系统字体目录递归查找（字体可能在子目录）
    for d in system_dirs:
        for name in candidates:
            for p in d.rglob(name):
                return p
    return None


def _find_any_font() -> Path | None:
    """最后兜底：bundled 目录或系统目录中的任意字体文件。

    仅用于"所有具名候选都没命中"时，仍保证有一个**真实字体**可用（字号才能生效）。
    真的一无所有才回退 `ImageFont.load_default()`（并告警）。
    """
    for base in [fonts_dir(), *_system_font_dirs()]:
        if not base.exists():
            continue
        for pattern in ("*.ttf", "*.otf", "*.ttc"):
            for p in base.rglob(pattern):
                return p
    return None


def load_font(
    size: int,
    bold: bool = False,
    rtl: bool = False,
    text: str = "",
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按优先级加载字体，找不到则回退 PIL 默认字体（并打 ERROR）。

    `text` 用于判断文种：含 CJK 字符时优先中文字体，否则优先拉丁字体。
    不传 `text`（默认空串）走**拉丁**路径——绝大多数文案是英文，这样也保证
    英文标题能拿到真正的粗体（不会被中文字体的 Regular 顶掉）。
    """
    px = max(1, int(size))
    cjk = bool(_CJK_RE.search(text or ""))

    order: list[list[str]] = []
    if rtl:
        order.append(ARABIC_BOLD_CANDIDATES if bold else ARABIC_REGULAR_CANDIDATES)
        order.append(ARABIC_REGULAR_CANDIDATES)
    if cjk:
        order.append(CJK_BOLD_CANDIDATES if bold else CJK_REGULAR_CANDIDATES)
        order.append(CJK_REGULAR_CANDIDATES)
        order.append(LATIN_BOLD_CANDIDATES if bold else LATIN_REGULAR_CANDIDATES)
    else:
        order.append(LATIN_BOLD_CANDIDATES if bold else LATIN_REGULAR_CANDIDATES)
        # 粗体文件缺失时，宁可回退常规体，也不要换成中文字体（保住文种一致性）
        order.append(LATIN_REGULAR_CANDIDATES if bold else LATIN_BOLD_CANDIDATES)
        order.append(CJK_REGULAR_CANDIDATES)
    order.append(ARABIC_REGULAR_CANDIDATES)

    seen: set[tuple[str, ...]] = set()
    for cands in order:
        key = tuple(cands)
        if key in seen:
            continue
        seen.add(key)
        path = _find_font(cands)
        if not path:
            continue
        try:
            return ImageFont.truetype(str(path), px)
        except Exception:  # noqa: BLE001  个别 ttc 需 index，失败换下一个候选
            continue

    any_font = _find_any_font()
    if any_font:
        try:
            return ImageFont.truetype(str(any_font), px)
        except Exception:  # noqa: BLE001
            pass

    _warn_fallback("load_font")
    return ImageFont.load_default()


def load_serif_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载衬线体（杂志版式标题用）。

    CJK 文本仍走中文字体（衬线拉丁字体不含汉字，会导致豆腐块），
    因此只有当调用方明确需要拉丁衬线风格时才使用本函数。
    """
    path = _find_font(SERIF_FONT_CANDIDATES)
    if path:
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:  # noqa: BLE001  个别 ttc 需要 index，失败则回退
            pass
    return load_font(size, bold=True)


# ---------------------------------------------------------------- 自检 / 诊断
def font_status() -> dict[str, Any]:
    """返回当前字体解析结果，供启动自检与健康检查使用。

    `ok=False` 意味着出图文字会退化为固定 10px（严重线上事故）。
    """
    probe = load_font(32, bold=True, text="Ag")
    probe_size = int(getattr(probe, "size", 0) or 0)
    latin_reg = _find_font(LATIN_REGULAR_CANDIDATES)
    latin_bold = _find_font(LATIN_BOLD_CANDIDATES)
    cjk = _find_font(CJK_REGULAR_CANDIDATES)
    any_font = _find_any_font()
    return {
        "ok": probe_size >= 32,
        "probe_size": probe_size,
        "assets_dir": str(fonts_dir()),
        "assets_dir_exists": fonts_dir().exists(),
        "latin_regular": str(latin_reg) if latin_reg else "",
        "latin_bold": str(latin_bold) if latin_bold else "",
        "cjk_regular": str(cjk) if cjk else "",
        "any": str(any_font) if any_font else "",
    }


def describe_font_status() -> str:
    """一行可读的字体状态描述（写日志用）。"""
    st = font_status()
    if st["ok"]:
        return (
            f"字体就绪：latin={st['latin_bold'] or st['latin_regular'] or '?'}"
            f", cjk={st['cjk_regular'] or '无'}"
        )
    return (
        f"🚨 字体缺失：load_font(32) 实测返回 size={st['probe_size']}（应为 32），"
        f"出图文字将全部变成 10px！assets_dir={st['assets_dir']}"
        f"(exists={st['assets_dir_exists']}), 任意可用字体={st['any'] or '无'}"
    )


def reshape_rtl(text: str) -> str:
    """阿拉伯文 RTL 重排（可选依赖，缺失时退化为反转）。"""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001
        return text[::-1]
