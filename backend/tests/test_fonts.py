"""字体加载回归测试。

背景（2026-10-12 线上事故）：`load_font()` 找不到字体文件时会**静默**回退
`ImageFont.load_default()`，而该字体**固定 10px、完全忽略传入的 size**，
导致整条出图链路的文字全都变成 10px。slim 基镜默认不含字体，问题就这样
无声上线了。

因此本文件守两件事：
1. 环境里**真的有字体**（`font_status().ok`）；
2. `load_font(px)` 返回的字体**真的按 px 缩放**（宽度随笔号增长），
   而不是那个"永远 10px"的默认字体。
"""
from __future__ import annotations

from app.image.fonts import load_font, font_status


def test_environment_has_real_fonts():
    st = font_status()
    assert st["ok"], (
        "字体自检未通过：load_font(32) 实测 size="
        f"{st['probe_size']}（应为 32）。出图文字会全部变成 10px。详情={st}"
    )


def test_load_font_respects_requested_size():
    """字号必须真正生效——默认字体会忽略 size，这正是事故的根因。"""
    for px in (14, 24, 32, 64, 140):
        f = load_font(px, bold=True, text="Ag")
        assert getattr(f, "size", 0) == px, (
            f"load_font({px}) 实际 size={getattr(f, 'size', None)}；"
            "疑似回退到了忽略字号的默认字体"
        )


def test_glyph_width_scales_with_size():
    """宽度随笔号线性增长（比断言 size 属性更难被"假字体"糊弄）。"""
    small = load_font(20, bold=True, text="Noise-canceling mic").getlength("N")
    large = load_font(80, bold=True, text="Noise-canceling mic").getlength("N")
    assert large > small * 3, f"字宽未随字号缩放：{small} -> {large}"


def test_bold_and_regular_are_distinct_files():
    """粗体与常规体应解析到不同字体文件（旧版候选顺序会让两者都命中 Bold）。"""
    st = font_status()
    if not st["latin_bold"] or not st["latin_regular"]:
        return  # 该环境只有单一字重的拉丁字体，跳过（不算失败）
    assert st["latin_bold"] != st["latin_regular"]


def test_cjk_text_prefers_cjk_font():
    """含中文时优先中文字体；若环境没装中文字体则跳过。"""
    st = font_status()
    if not st["cjk_regular"]:
        return
    f = load_font(32, bold=False, text="无线降噪耳机")
    assert getattr(f, "size", 0) == 32
