"""字体加载与文本渲染（支持 CJK / 阿拉伯 RTL）。

搜索顺序：bundled assets/fonts -> 系统字体目录 -> 递归查找。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import ImageFont

from app.core.config import settings

# 常见中文字体文件名（按优先级）
CJK_FONT_CANDIDATES = [
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJKsc-Regular.otf",
    "NotoSansSC-Regular.otf",
    "SourceHanSansSC-Regular.otf",
    "SourceHanSansCN-Regular.otf",
    "msyh.ttc",
    "msyhbd.ttc",
    "msyh.ttf",
    "simhei.ttf",
    "simsun.ttc",
    "PingFang.ttc",
    "PingFang-SC-Regular.otf",
    "Arial Unicode.ttf",
]
ARABIC_FONT_CANDIDATES = [
    "NotoSansArabic-Regular.ttf",
    "NotoNaskhArabic-Regular.ttf",
    "Amiri-Regular.ttf",
]
LATIN_FONT_CANDIDATES = [
    "DejaVuSans-Bold.ttf",
    "DejaVuSans.ttf",
    "arialbd.ttf",
    "arial.ttf",
    "Arial.ttf",
    "Roboto-Regular.ttf",
    "Roboto-Bold.ttf",
]

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


def fonts_dir() -> Path:
    return Path(settings.assets_dir) / "fonts"


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
    """按优先级在 bundled 目录与系统字体目录中查找字体。"""
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

    # 4) 兜底：系统目录里任意 ttf/otf/ttc（保证至少有字体可用）
    for d in system_dirs:
        for pattern in ("*.ttf", "*.otf", "*.ttc"):
            for p in d.rglob(pattern):
                return p
    return None


def load_font(size: int, bold: bool = False, rtl: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按优先级加载字体，找不到则回退 PIL 默认字体。"""
    if rtl:
        path = _find_font(ARABIC_FONT_CANDIDATES)
        if path:
            return ImageFont.truetype(str(path), size)
    cjk = _find_font(CJK_FONT_CANDIDATES)
    if cjk:
        return ImageFont.truetype(str(cjk), size)
    latin = _find_font(LATIN_FONT_CANDIDATES)
    if latin:
        return ImageFont.truetype(str(latin), size)
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


def reshape_rtl(text: str) -> str:
    """阿拉伯文 RTL 重排（可选依赖，缺失时退化为反转）。"""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001
        return text[::-1]
