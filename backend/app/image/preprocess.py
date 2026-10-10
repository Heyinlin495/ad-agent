"""产品图片预处理：格式校验、EXIF 旋转、压缩、去背景、质量检测。"""
from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache

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
#
# JPEG/MPO 另有 DCT 缩放解码（见 _JPEG_DECODE_MAX_DIM）：可在 load() 前让 libjpeg
# 直接按 1/2、1/4、1/8 降采样，解码内存与像素数解耦，故对 JPEG 放宽到 80MP，
# 覆盖 48/50/64MP 手机直出（后续 compress_image 反正只保留 1600px）。
# PNG/WebP 无 draft 能力，只能整幅解码，仍按 40MP 收紧。
_PIXEL_LIMIT_JPEG = 80_000_000
_PIXEL_LIMIT_OTHER = 40_000_000
_JPEG_FORMATS = {"JPEG", "MPO"}

# JPEG draft 目标边长：与 compress_image 的 1600 留出余量，避免二次缩放损失。
# 实测 Pillow 11 仅对 RGB 彩色 JPEG 生效，灰度/CMYK 为 no-op（这两者内存占用本就低）。
_JPEG_DECODE_MAX_DIM = 3000

# PIL 自身的兜底阈值（超 2× 才硬报错）。真实闸门是 validate_image 的显式校验，
# 这里放宽以免大图在 draft 降采样之前就被 PIL 拦掉。
Image.MAX_IMAGE_PIXELS = 200_000_000


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
        is_jpeg_like = (img.format or "").upper() in _JPEG_FORMATS
        pixel_limit = _PIXEL_LIMIT_JPEG if is_jpeg_like else _PIXEL_LIMIT_OTHER
        if pw * ph > pixel_limit:
            raise ImageProcessError(
                f"图片像素过大（{pw}x{ph}），请压缩后重试"
            )
        if is_jpeg_like:
            # 大图降采样解码：48MP 手机原图整幅解码要约 150MB，draft 后 libjpeg
            # 只解 1/2~1/8，而下游 compress_image 最终只保留 1600px，画质无实际损失。
            img.draft(img.mode, (_JPEG_DECODE_MAX_DIM, _JPEG_DECODE_MAX_DIM))
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


_rembg_lock = threading.Lock()

# 抠图失败后的冷却期：失败（含模型下载中断）不会写入 lru_cache（lru_cache 不缓存
# 异常），若不设冷却，每个识别请求都会重复触发一次数百 MB 的模型下载/加载重试。
_REMBG_FAIL_COOLDOWN_SEC = 300
_rembg_failed_at: dict[str, float] = {}


def _rembg_home() -> str:
    """rembg 模型缓存目录（与 rembg BaseSession.u2net_home 的解析保持一致）。"""
    return os.path.expanduser(
        os.getenv("U2NET_HOME")
        or os.path.join(os.getenv("XDG_DATA_HOME", "~"), ".u2net")
    )


def _purge_stale_rembg_tmp(max_age_sec: int = 600) -> None:
    """清理 rembg(pooch) 中断下载残留的 tmp* 文件。

    pooch 先把模型下到 ``tmpXXXX``、校验 md5 后再改名落地；下载被中断（网络超时 /
    进程重启）时临时文件会残留，反复重试持续堆积（线上曾积到 3.4GB 打满磁盘）。
    只删除 mtime 超过 ``max_age_sec`` 的 tmp 文件，避免误删正在进行的下载。
    """
    home = _rembg_home()
    try:
        now = time.time()
        for name in os.listdir(home):
            if not name.startswith("tmp"):
                continue
            path = os.path.join(home, name)
            try:
                if now - os.path.getmtime(path) > max_age_sec:
                    os.remove(path)
                    logger.info(f"清理残留的模型临时文件: {path}")
            except OSError:
                pass
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"清理模型临时文件失败: {exc}")


@lru_cache(maxsize=4)
def _load_rembg_session(name: str):
    from rembg import new_session

    _purge_stale_rembg_tmp()
    return new_session(name)


def _rembg_session(name: str):
    """进程内缓存 rembg 推理会话（按模型名），并用锁串行化首次创建。

    ⚠ 缓存函数必须定义在**模块级**：早期实现把 ``_session`` 连同 ``@lru_cache``
    定义在 ``remove_background`` 函数体内，装饰器随每次调用重建、缓存永远为空，
    于是每个识别请求都要重新初始化一遍 ONNX 会话，这是识别慢的头号原因。

    ⚠ 外层再加 ``threading.Lock``：``lru_cache`` 不保证并发首次调用只执行一次，
    多请求同时到达时会各自触发一次模型下载/加载（线上曾并发下 4~5 份 973MB 模型）。
    """
    with _rembg_lock:
        return _load_rembg_session(name)


# 抠图模型多级策略。**刻意不使用 birefnet-general**：其 onnx 达 973MB，在生产机
# （2 核 2GB、出口带宽约 1MB/s）下载动辄十几分钟且极易中断，推理也远超该配置的
# 内存/算力预算。改用轻量档：
#   u2net（约 176MB，320x320 输入，CPU 上快）→ isnet-general-use（约 176MB，备选）。
# 首个成功即返回，后续档位仅在前档加载/推理失败时触发。
_REMBG_MODELS = ("u2net", "isnet-general-use")


def _model_in_cooldown(name: str) -> bool:
    ts = _rembg_failed_at.get(name)
    return ts is not None and (time.time() - ts) < _REMBG_FAIL_COOLDOWN_SEC


def remove_background(img: Image.Image) -> Image.Image:
    """使用 rembg 去除背景，输出 RGBA 透明主体图。

    多级模型策略见 ``_REMBG_MODELS``；会话进程内缓存（见 ``_rembg_session``），
    抠图后做主体碎片清理；全部失败时降级返回原图（白色背景）。
    失败的模型会进入冷却期，避免每个请求重复触发大模型的下载/加载。
    """
    last_exc: Exception | None = None
    for model in _REMBG_MODELS:
        if _model_in_cooldown(model):
            continue
        try:
            from rembg import remove

            out = remove(img, session=_rembg_session(model))
            return _keep_main_subject(out.convert("RGBA"))
        except Exception as exc:  # noqa: BLE001
            _rembg_failed_at[model] = time.time()
            logger.warning(
                f"rembg 模型 {model} 抠图失败（进入 {_REMBG_FAIL_COOLDOWN_SEC}s 冷却），"
                f"尝试下一档: {exc}"
            )
            last_exc = exc
    logger.warning(f"rembg 无可用的抠图模型，降级为原图: {last_exc}")
    return img.convert("RGBA")


def warmup_rembg() -> None:
    """后台预热首个可用抠图模型的会话（幂等，失败静默）。

    在生产这类内存/CPU 受限的机器上，ONNX 会话**首次**初始化可达分钟级
    （实测 2 核 2GB 机器上 u2net 冷启动 ~165s，主要耗在内存紧张引起的 swap）。
    若不预热，部署/重启后的**第一个**用户请求会独自承担这段耗时并几乎必然超时。
    由 ``app.main.lifespan`` 在启动时放入后台线程调用，把这段成本挪到进程启动期；
    预热结果写入 ``lru_cache``，后续请求直接命中。
    """
    for model in _REMBG_MODELS:
        if _model_in_cooldown(model):
            continue
        try:
            _rembg_session(model)
            logger.info(f"rembg 预热完成：{model} 会话已就绪")
            return
        except Exception as exc:  # noqa: BLE001
            _rembg_failed_at[model] = time.time()
            logger.warning(f"rembg 预热 {model} 失败，尝试下一档：{exc}")
    logger.warning("rembg 预热失败：全部模型不可用（首个请求将走降级路径）")


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
