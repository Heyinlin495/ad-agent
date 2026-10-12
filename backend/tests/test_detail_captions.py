"""细节四格回归：① 标题防"参数幻觉"；② 标题必须驱动取景；③ 图生图锁商品/背景；④ 雷同闸门。

背景（2026-10-12 实测缺陷）：同一张海报里，主视觉是无麦克风杆的耳机，细节四格却
出现带 boom mic、银色圆铰链的**不同款**——"一图五款"。成因是细节图走的是**独立
文生图**（只喂"颜色+材质+商品名"），与主图（通道 A 整图重塑）各自想象商品形态。

第二处缺陷（同日实测）：修完一致性后发现四格**是同机位**——两两相关均值 +0.78、
掩膜质心全部落在 (0.48~0.50, 0.61)、主轴角 164~175°。成因是四条"角度"指令互为
同义改写，且**标题点名的部件从没进过提示词**。现在标题驱动取景 + 出图后雷同闸门。
"""
from __future__ import annotations

import io

from app.agents.nodes import (
    _detail_caption_candidates,
    _detail_edit_instructions,
    _detail_prompts,
    _focus_phrase,
    _resolve_detail_captions,
    _spec_tokens,
)
from app.image.detail_tiles import (
    deterministic_detail_crops,
    duplicate_pairs,
    tile_signature,
)

_CAPS = ["PU Leather Cushions", "Metal Adjustment Rails", "Reinforced Boom Arm", "In the Box"]


def _png(color: tuple[int, int, int], size: tuple[int, int] = (300, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_spec_tokens_extracts_spec_like_words():
    toks = _spec_tokens("Dual 3.5mm & USB, Bluetooth 5.0, 5000mAh")
    for expect in ("3.5mm", "usb", "bluetooth5.0", "5000mah"):
        assert expect in toks, f"未识别出规格词 {expect}：{toks}"


def test_hallucinated_spec_caption_is_replaced():
    """商品信息里没有 USB / 3.5mm，标题却写了 → 必须弃用，改用通用兜底标题。"""
    product = {"name": "Wireless Headphones", "material": "PU leather", "color": "black"}
    caps = _resolve_detail_captions(
        product,
        ["Textured Surface", "Metal Adjustment Rails", "Reinforced Boom Arm", "Dual 3.5mm & USB"],
        set(),
        4,
    )
    assert caps[0] == "Textured Surface"
    assert caps[3] == "In the Box", f"编造参数的标题未被替换: {caps}"
    assert all("3.5mm" not in c and "usb" not in c.lower() for c in caps)


def test_grounded_spec_caption_is_kept():
    """商品信息里确实写了 USB-C / 3.5mm，则含它的标题应保留。"""
    product = {"name": "Headset", "description": "wired headset with USB-C and 3.5mm jack"}
    caps = _resolve_detail_captions(
        product,
        ["USB-C Port", "Textured Surface", "Reinforced Build", "Included Accessories"],
        set(),
        4,
    )
    assert caps[0] == "USB-C Port", f"有据可依的标题被误杀: {caps}"


def test_caption_duplicating_bullet_is_replaced():
    """细节标题与卖点标题重复时必须替换（同一张海报不得出现两处相同文字）。"""
    caps = _resolve_detail_captions(
        {"name": "Headset"}, ["Clear Sound", "B", "C", "D"], {"clear sound"}, 4
    )
    assert caps[0] == "Premium Finish"


def test_detail_edit_instructions_lock_product_and_background():
    """4 条图生图指令：必须锁死"商品完全不变"、统一"浅灰影棚背景"、且不含品类词。"""
    instrs = _detail_edit_instructions(_CAPS)
    assert len(instrs) == 4
    for s in instrs:
        assert "完全不变" in s, "未锁定商品形态"
        assert "浅灰" in s, "未统一背景"
        for bad in ("耳机", "headphone", "earbud", "鼠标", "mouse"):
            assert bad not in s.lower(), f"指令里混入了品类词 {bad}，会误伤其它品类"


def test_captions_drive_the_framing():
    """🚨 标题点名的部件必须进入出图提示词与编辑指令 —— 这是「四格同机位」的正面修法。

    旧版提示词只有 `{color} {material} {name}`，"PU Leather Cushions" 里的
    cushion 从没出现在提示词中，模型无从知道该拍哪个局部。
    """
    product = {"name": "Headphones", "material": "PU leather", "color": "black"}
    specs = _detail_prompts(product, _CAPS)
    instrs = _detail_edit_instructions(_CAPS)
    assert len(specs) == 4 and len(instrs) == 4

    assert "cushions" in specs[0][1].lower(), "标题部件名没进提示词"
    assert "rails" in specs[1][1].lower()
    assert "boom arm" in specs[2][1].lower()
    for cap, instr in zip(_CAPS, instrs):
        assert cap.lower() in instr.lower(), f"编辑指令里没有标题部件: {instr}"

    # 四条提示词必须彼此不同，且都要求"不要整机宽景"
    prompts = [p for _t, p in specs]
    assert len(set(prompts)) == 4, "四条提示词重复，模型只会给同一张图"
    for p in prompts:
        low = p.lower()
        assert "do not show the whole product" in low
        assert "no full-product wide shot" in low


def test_retry_instructions_ask_for_a_different_camera():
    """雷同闸门重试用的指令必须显式要求换机位，否则重试也是白重试。"""
    base = _detail_edit_instructions(_CAPS)
    retry = _detail_edit_instructions(_CAPS, retry=True)
    assert len(retry) == 4
    for b, r in zip(base, retry):
        assert r.startswith(b[:20])
        assert "换一个明显不同的机位" in r, "重试指令没有要求换机位"


def test_focus_phrase_falls_back_when_caption_is_junk():
    """标题不可用（空/纯符号）时必须用兜底取景短语，不能产生空提示词。"""
    assert _focus_phrase("", "fallback phrase") == "fallback phrase"
    assert _focus_phrase("!!!", "fallback phrase") == "fallback phrase"
    assert "cushion" in _focus_phrase("PU Leather Cushions!", "x")


def test_caption_candidates_fall_back_to_bullets():
    design = {"_bullets": ["A|a", "B|b", "C|c", "D|d", "E|e"]}
    assert _detail_caption_candidates({}, design) == ["A", "B", "C", "D"]
    assert _detail_caption_candidates({}, {}) == [
        "Premium Finish", "Fine Craftsmanship", "Built to Last", "In the Box",
    ]


def test_gate_flags_four_identical_tiles():
    """四张一模一样的图必须被判为雷同（生产实测四格就是这种情形）。"""
    same = _png((150, 150, 150))
    sigs = [tile_signature(same) for _ in range(4)]
    pairs = duplicate_pairs(sigs)
    assert len(pairs) == 6, f"四张相同图应产生 6 对雷同: {pairs}"


def test_deterministic_crops_are_not_duplicates():
    """兜底裁切必须产出互不相同的局部（这是雷同闸门的最后一道防线）。"""
    from PIL import Image, ImageDraw

    src = Image.new("RGB", (900, 600), (200, 200, 200))
    d = ImageDraw.Draw(src)
    for i, box in enumerate(((60, 60, 300, 240), (520, 90, 840, 300), (360, 380, 620, 560))):
        d.rectangle(box, fill=(40 + i * 20, 40, 40))
    buf = io.BytesIO()
    src.save(buf, format="PNG")

    crops = deterministic_detail_crops(buf.getvalue(), 4, 1.5)
    assert len(crops) == 4
    sizes = {Image.open(io.BytesIO(c)).size for c in crops}
    assert len(sizes) == 1, f"四张裁剪尺寸应一致: {sizes}"
    assert all(abs(w / h - 1.5) < 0.02 for w, h in sizes), f"裁剪画幅不是 3:2: {sizes}"
    sigs = [tile_signature(c) for c in crops]
    assert not duplicate_pairs(sigs), f"兜底裁切仍然雷同: {duplicate_pairs(sigs)}"
