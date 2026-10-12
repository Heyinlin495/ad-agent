"""细节四格的「多样性」工具：签名、雷同判定、确定性区域裁切。

🚨 背景（2026-10-12 生产图实测）：海报细节四格两两相关均值 **+0.78**（最高 +0.89），
商品掩膜质心全部落在 (0.48~0.50, 0.61)、主轴角 164~175°、掩膜占比 27~31% —— 四格
其实是**同一张整机图**，与"材质 / 结构 / 耐用 / 配件"四个特写标题完全不符。

根因是两侧都没有把关：生成侧四条指令互为同义改写（都只说"推近到……"），渲染侧又把
1:1 出图裁成 2.15:1（纵向裁掉 53.6%）。本模块提供三件东西：

- ``tile_signature()``：把一张图压成（掩膜占比, 质心, 主轴角, 细长比）+ 缩略灰度向量；
- ``duplicate_pairs()``：判定哪几格互为雷同（阈值取实测值的保守侧）；
- ``deterministic_detail_crops()`` / ``detail_base_variants()``：确定性地裁出
  互不相同的局部，前者作细节带的**最后兜底**，后者作图生图的**不同起点**。
"""

from __future__ import annotations

import io
import logging
from typing import NamedTuple, Sequence

logger = logging.getLogger(__name__)

# 细节格的目标画幅（宽, 高）。**出图端与渲染端必须一致**：
# `generator.SIZE_PRESETS["detail"]` = 1200×800（万相按 `_wanx_size` → 1440×960），
# `poster._draw_detail_strip` 按同一个比例算格高。两边不一致就会在渲染时中心裁切。
DETAIL_ASPECT: tuple[int, int] = (3, 2)
DETAIL_ASPECT_RATIO: float = DETAIL_ASPECT[0] / DETAIL_ASPECT[1]

# 商品（深色主体）灰度阈值：与海报细节带的量测口径保持一致
_LUMA_DARK = 118

# 雷同判据。生产四格的缩略相关实测 0.73~0.89，取 0.88 为阈值：
# 高于它基本就是同一张图；低于它再用「主轴角 + 质心 + 占比」三重几何判据兜一层。
_CORR_SAME = 0.88
_AXIS_SAME_DEG = 12.0
_CENTROID_SAME = 0.05
_COVER_SAME = 0.04


class TileSignature(NamedTuple):
    """一张细节图的可比较签名。"""

    cover: float   # 商品掩膜占比 0~1
    cx: float      # 掩膜质心 x（归一化）
    cy: float      # 掩膜质心 y（归一化）
    axis: float    # 主轴角（度，-180~180，无向）
    elong: float   # 细长比
    vec: object    # 归一化缩略灰度向量（去均值，供相关系数用）


# 兜底裁切窗口：(缩放, 中心 x, 中心 y)，均归一化。刻意做出**不同的缩放 + 不同的偏心**，
# 保证四张的掩膜占比与质心天然不同，不会再被判为雷同。
_FALLBACK_WINDOWS: tuple[tuple[float, float, float], ...] = (
    (0.55, 0.50, 0.50),
    (0.82, 0.33, 0.36),
    (0.82, 0.67, 0.42),
    (0.66, 0.50, 0.70),
)

# 图生图**底图**的取景窗口：比兜底温和得多（商品须完整可见，只微调取景起点），
# 目的是让四张编辑的输入不同 —— 即使模型很保守，也不会产出四张一模一样的图。
_BASE_WINDOWS: tuple[tuple[float, float, float], ...] = (
    (0.94, 0.50, 0.50),
    (0.86, 0.36, 0.38),
    (0.86, 0.64, 0.42),
    (0.80, 0.50, 0.64),
)


def tile_signature(image_bytes: bytes, grid: tuple[int, int] = (48, 32)) -> TileSignature | None:
    """把一张图压成签名；解码失败返回 None（调用方按"不可比较"处理，不误判）。"""
    try:
        from PIL import Image
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"细节图签名依赖不可用: {exc}")
        return None

    try:
        gray = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"细节图签名解码失败: {exc}")
        return None

    small = np.asarray(gray.resize(grid), dtype=np.float32).ravel()
    small = small - small.mean()
    norm = float(np.linalg.norm(small)) or 1.0
    vec = small / norm

    a = np.asarray(gray, dtype=np.float32)
    mask = a < _LUMA_DARK
    cover = float(mask.mean())
    ys, xs = np.nonzero(mask)
    if xs.size < 24:            # 几乎没有深色主体（例如纯渐变兜底）→ 几何量为默认值
        return TileSignature(cover, 0.5, 0.5, 0.0, 1.0, vec)

    h, w = a.shape
    dx = xs - xs.mean()
    dy = ys - ys.mean()
    cov = np.array(
        [
            [float((dx * dx).mean()), float((dx * dy).mean())],
            [float((dx * dy).mean()), float((dy * dy).mean())],
        ]
    )
    evals, evecs = np.linalg.eigh(cov)
    axis = float(np.degrees(np.arctan2(evecs[1, -1], evecs[0, -1])))
    elong = float((max(evals[-1], 0.0) / max(evals[-2], 1e-6)) ** 0.5)
    return TileSignature(cover, float(xs.mean()) / w, float(ys.mean()) / h, axis, elong, vec)


def _axis_gap(a: float, b: float) -> float:
    """主轴是**无向**的（差 180° 等价），取环形最小夹角。"""
    return abs(((a - b + 90.0) % 180.0) - 90.0)


def tiles_too_similar(a: TileSignature | None, b: TileSignature | None) -> bool:
    """两格是否雷同。任一签名不可用 → 判为"不雷同"（宁可放过，不误伤）。"""
    if a is None or b is None:
        return False
    import numpy as np

    if float(np.dot(a.vec, b.vec)) >= _CORR_SAME:
        return True
    dist = ((a.cx - b.cx) ** 2 + (a.cy - b.cy) ** 2) ** 0.5
    return (
        _axis_gap(a.axis, b.axis) < _AXIS_SAME_DEG
        and dist < _CENTROID_SAME
        and abs(a.cover - b.cover) < _COVER_SAME
    )


def duplicate_pairs(sigs: Sequence[TileSignature | None]) -> list[tuple[int, int]]:
    """返回所有互为雷同的格位下标对。"""
    out: list[tuple[int, int]] = []
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            if tiles_too_similar(sigs[i], sigs[j]):
                out.append((i, j))
    return out


def _crop_windows(
    image_bytes: bytes,
    n: int,
    aspect: float,
    windows: Sequence[tuple[float, float, float]],
    max_side: int = 1024,
) -> list[bytes]:
    """按 (缩放, 中心x, 中心y) 窗口裁剪 n 个**宽高比 = aspect** 的局部，返回 JPEG 字节。"""
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"细节图裁切依赖不可用: {exc}")
        return []

    try:
        src = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"细节图底图解码失败，跳过确定性裁切: {exc}")
        return []

    w_src, h_src = src.size
    if w_src < 64 or h_src < 64:
        return []

    out: list[bytes] = []
    # 🚨 **统一输出尺寸**：以最小的取景窗口为基准算出目标像素尺寸，四张裁切统一
    # resize 到该尺寸。取景范围（视场）仍然各不相同 —— 多样性不受影响；
    # 但拼成细节带时四张卡片的分辨率/锐度一致（旧版 4 张分别是 495×330 / 738×492 /
    # 594×396，肉眼能看出"有的更清楚"）。
    zooms = [windows[k % len(windows)][0] for k in range(n)]
    tw = int(w_src * min(zooms)) if zooms else w_src
    th = int(round(tw / aspect))
    if th > h_src:
        th, tw = h_src, int(round(h_src * aspect))
    tw = max(48, min(w_src, tw))
    th = max(48, min(h_src, th))
    if max(tw, th) > max_side:
        sc = max_side / max(tw, th)
        tw, th = max(1, int(tw * sc)), max(1, int(th * sc))

    for k in range(n):
        zoom, cxr, cyr = windows[k % len(windows)]
        cw = max(tw, min(w_src, int(w_src * zoom)))
        ch = max(th, min(h_src, int(round(cw / aspect))))
        x0 = int(min(max(0.0, cxr * w_src - cw / 2.0), w_src - cw))
        y0 = int(min(max(0.0, cyr * h_src - ch / 2.0), h_src - ch))
        cell = src.crop((x0, y0, x0 + cw, y0 + ch)).resize((tw, th), Image.LANCZOS)
        buf = io.BytesIO()
        cell.save(buf, format="JPEG", quality=90)
        out.append(buf.getvalue())
    return out


def deterministic_detail_crops(
    image_bytes: bytes,
    n: int = 4,
    aspect: float = 1.5,
) -> list[bytes]:
    """从上图确定性地裁出 n 个**互不相同**的局部 —— 细节带的最后兜底。"""
    return _crop_windows(image_bytes, n, aspect, _FALLBACK_WINDOWS)


def detail_base_variants(
    image_bytes: bytes,
    n: int = 4,
    aspect: float = 1.5,
    max_side: int = 1024,
) -> list[bytes]:
    """给图生图用的 n 份「起点不同」的底图（温和取景，商品保持完整）。"""
    return _crop_windows(image_bytes, n, aspect, _BASE_WINDOWS, max_side=max_side)
