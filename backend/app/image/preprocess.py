"""产品图片预处理：格式校验、EXIF 旋转、压缩、去背景、质量检测。"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageOps
from loguru import logger

from app.core.config import settings
from app.schemas.product import QualityReport

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_SIZE_MB = settings.max_upload_size_mb

# 解压炸弹防护：只限制压缩后字节数不够——一个 10MB 的 PNG 可解压成数亿像素，
# 在 Image.load() 阶段就会吃掉数 GB 内存打爆 worker。这里按总像素数设上限。
Image.MAX_IMAGE_PIXELS = 40_000_000  # 4000 万像素（约 6300x6300）


class ImageProcessError(Exception):
    pass


def validate_image(data: bytes, filename: str) -> tuple[Image.Image, str]:
    """校验格式与大小，返回 (PIL.Image, mime_type)。

    白名单格式（jpg/png/webp）原样返回；MPO/BMP/GIF 等 PIL 可解码的格式
    （如相机多帧 MPO，取首帧）统一转换为 RGB JPEG 接管，避免前端缓存旧代码时
    原始文件直达后端被拒。
    """
    if len(data) > MAX_SIZE_MB * 1024 * 1024:
        raise ImageProcessError(f"图片超过 {MAX_SIZE_MB}MB 限制")
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    try:
        img = Image.open(io.BytesIO(data))
        # 先读尺寸再 load()：Image.open 是惰性的，此时才知真实像素数，
        # 可在解码前拦截解压炸弹（否则 load() 已经分配了巨量内存）。
        pw, ph = img.size
        if pw * ph > Image.MAX_IMAGE_PIXELS:
            raise ImageProcessError(
                f"图片像素过大（{pw}x{ph}），请压缩后重试"
            )
        img.load()
    except ImageProcessError:
        raise
    except Image.DecompressionBombError as exc:
        # PIL 自身对超过上限 2 倍的图会在 open() 直接抛错
        raise ImageProcessError("图片像素过大，疑似解压炸弹，请压缩后重试") from exc
    except Exception as exc:  # noqa: BLE001
        raise ImageProcessError(f"无法解析图片: {exc}") from exc
    fmt = (img.format or "").lower()
    if fmt in {"jpeg", "png", "webp"} and (not ext or ext in ALLOWED_EXTS):
        return img, f"image/{fmt}"
    try:
        img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise ImageProcessError(f"不支持的图片格式: {ext or (img.format or '未知')}") from exc
    logger.info(f"图片格式 {ext or img.format} 已自动转换为 JPEG")
    return img, "image/jpeg"


def apply_exif_orientation(img: Image.Image) -> Image.Image:
    """按 EXIF 信息旋转纠正。"""
    return ImageOps.exif_transpose(img)


def compress_image(img: Image.Image, max_dim: int = 1600) -> Image.Image:
    """等比压缩到最长边 max_dim，控制体积。"""
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_dim / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def _keep_main_subject(rgba: Image.Image, iterations: int = 5) -> Image.Image:
    """剔除与主体仅窄点相连/分离的杂物碎片：腐蚀断开细连接 → 取最大连通域
    （及面积可观的并列主体）→ 膨胀回原尺寸与原掩码相交。

    对"手+产品"实拍图中残留的桌面小物体、独立碎片有效；与主体大面积
    交叉的入镜物（如穿过产品的线缆）无法分离，属输入图歧义。
    OpenCV 不可用时原样返回。
    """
    try:
        import cv2

        alpha = np.array(rgba.getchannel("A"))
        mask = (alpha > 10).astype(np.uint8)
        kernel = np.ones((3, 3), np.uint8)
        eroded = cv2.erode(mask, kernel, iterations=iterations)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(eroded, 8)
        if n <= 2:
            return rgba
        areas = stats[1:, cv2.CC_STAT_AREA]
        main = 1 + int(np.argmax(areas))
        keep = [main]
        for i in range(1, n):  # 并列主体（如一双鞋）面积可观则保留
            if i != main and stats[i, cv2.CC_STAT_AREA] > 0.08 * areas.max():
                keep.append(i)
        region = np.isin(labels, keep)
        region = cv2.dilate(region.astype(np.uint8), kernel, iterations=iterations)
        out_mask = (region > 0) & (mask > 0)
        alpha2 = np.where(out_mask, alpha, 0).astype(np.uint8)
        arr = np.array(rgba)
        arr[:, :, 3] = alpha2
        return Image.fromarray(arr)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"主体碎片清理失败，跳过后处理: {exc}")
        return rgba


def remove_background(img: Image.Image) -> Image.Image:
    """使用 rembg 去除背景，输出 RGBA 透明主体图。

    多级模型策略：birefnet-general（对复杂手持场景最稳）→ isnet-general-use → u2net。
    session 进程内缓存，避免重复加载模型；抠图后做主体碎片清理；
    全部失败时降级返回原图（白色背景）。
    """
    from functools import lru_cache

    @lru_cache(maxsize=4)
    def _session(name: str):
        from rembg import new_session

        return new_session(name)

    last_exc: Exception | None = None
    for model in ("birefnet-general", "isnet-general-use", "u2net"):
        try:
            from rembg import remove

            out = remove(img, session=_session(model))
            return _keep_main_subject(out.convert("RGBA"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"rembg 模型 {model} 抠图失败，尝试下一档: {exc}")
            last_exc = exc
    logger.warning(f"rembg 全部模型失败，降级为原图: {last_exc}")
    return img.convert("RGBA")


@dataclass
class QualityMetrics:
    blur_var: float = 0.0
    brightness: float = 0.0
    subject_ratio: float = 1.0


def _compute_metrics(img: Image.Image) -> QualityMetrics:
    """计算模糊度 / 亮度 / 主体占比（OpenCV 缺失时降级为 PIL 估算）。"""
    blur_var = 100.0
    try:
        import cv2

        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        blur_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(gray.mean())
    except Exception:  # noqa: BLE001  OpenCV 不可用时用 PIL 估算亮度
        from PIL import ImageStat

        stat = ImageStat.Stat(img.convert("L"))
        brightness = float(stat.mean[0])

    if img.mode == "RGBA":
        alpha = np.array(img.getchannel("A"))
        subject_ratio = float((alpha > 10).mean())
    else:
        gray = np.array(img.convert("L"))
        subject_ratio = float((gray < 245).mean())
    return QualityMetrics(blur_var, brightness, subject_ratio)


def detect_quality(img: Image.Image) -> QualityReport:
    """检测模糊、过暗、主体过小，返回质量报告。"""
    m = _compute_metrics(img)
    blurry = m.blur_var < 60
    too_dark = m.brightness < 40
    subject_too_small = m.subject_ratio < 0.08
    messages: list[str] = []
    if blurry:
        messages.append("图片较模糊，建议重新拍摄")
    if too_dark:
        messages.append("图片过暗，请增加光线")
    if subject_too_small:
        messages.append("产品主体占比过小，请靠近拍摄")
    return QualityReport(
        ok=not (blurry or too_dark or subject_too_small),
        blurry=blurry,
        too_dark=too_dark,
        subject_too_small=subject_too_small,
        messages=messages,
    )
