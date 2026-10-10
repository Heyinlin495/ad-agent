"""图像生成（可插拔）：OpenAI / 通义万相 / SD / Flux / mock。"""
from __future__ import annotations

import base64
import io
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings

# 复用一个 Client 以复用 TCP 连接（连接池）；各请求可单独覆盖 timeout
_http_client = httpx.Client(timeout=120)

# 尺寸预设（宽, 高）
SIZE_PRESETS: dict[str, tuple[int, int]] = {
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "amazon": (2000, 2000),
}


def resolve_size(size_preset: str) -> tuple[int, int]:
    return SIZE_PRESETS.get(size_preset, SIZE_PRESETS["1:1"])


def make_gradient_background(size: tuple[int, int], color_top: str = "#6EC6FF", color_bottom: str = "#E6F4FF") -> bytes:
    """生成柔和渐变背景（mock / 兜底）。"""
    from PIL import Image, ImageDraw

    w, h = size
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    top = _hex_to_rgb(color_top)
    bottom = _hex_to_rgb(color_bottom)
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def generate_image(
    prompt: str,
    size_preset: str,
    negative_prompt: str | None = None,
) -> bytes:
    """按配置调用图像模型生成背景图，返回 PNG 字节。

    mock 模式或无 Key 时返回渐变背景兜底。
    """
    provider = settings.image_provider
    size = resolve_size(size_preset)
    if provider == "mock" or settings.mock_mode:
        return make_gradient_background(size)

    try:
        if provider == "openai":
            return _generate_openai(prompt, size)
        if provider == "dashscope":
            return _generate_dashscope(prompt, size, negative_prompt)
        if provider in {"sd", "flux"}:
            return _generate_http(prompt, size, negative_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"图像生成失败，降级渐变背景: {exc}")
        return make_gradient_background(size)
    return make_gradient_background(size)


def _generate_openai(prompt: str, size: tuple[int, int]) -> bytes:
    # DALL·E 支持的尺寸为固定值，这里做近似映射
    mapping = {(1080, 1080): "1024x1024", (2000, 2000): "1024x1024"}
    size_str = mapping.get(size, "1024x1024")
    resp = _http_client.post(
        (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/") + "/images/generations",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={"model": settings.image_model or "dall-e-3", "prompt": prompt, "size": size_str, "n": 1},
        timeout=120,
    )
    resp.raise_for_status()
    b64 = resp.json()["data"][0]["b64_json"]
    return base64.b64decode(b64)


# 万相 2.1/2.2 系列：宽高均可取 [512, 1440]，且**支持任意长宽比**（非固定枚举）。
# 因此可以按目标画布的长宽比**等比**请求，而不是塌缩到 1024² / 1280×720 / 720×1280
# 三档——后者会把 4:5(0.80) 变成 720×1280(0.5625)，AI 按错误比例作画，构图与留白
# 位置全被改变，是"广告图不成海报"的主因。
_WANX_MIN = 512
_WANX_MAX = 1440


def _wanx_size(w: int, h: int) -> str:
    """将目标尺寸映射为通义万相支持的尺寸，**保持长宽比**。

    做法：以最长边贴到 _WANX_MAX(1440) 为基准等比缩放，再把两边夹到
    [_WANX_MIN, _WANX_MAX]，最后微调使总像素与长宽比都保持在合理范围。
    这样 4:5 → 1152×1440、9:16 → 810×1440、16:9 → 1440×810、1:1 → 1440×1440，
    长宽比与原画布一致，AI 的构图与留白位置不会再被比例错误破坏。
    """
    if w <= 0 or h <= 0:
        return "1024*1024"

    ratio = w / h
    if ratio >= 1:  # 横版或正方：宽为长边
        nw = _WANX_MAX
        nh = int(round(_WANX_MAX / ratio))
    else:           # 竖版：高为长边
        nh = _WANX_MAX
        nw = int(round(_WANX_MAX * ratio))

    nw = max(_WANX_MIN, min(_WANX_MAX, nw))
    nh = max(_WANX_MIN, min(_WANX_MAX, nh))
    # 万相按 8 像素对齐更稳（非强制，但可避免个别尺寸被拒）
    nw -= nw % 8
    nh -= nh % 8
    return f"{nw}*{nh}"


def _generate_dashscope(
    prompt: str,
    size: tuple[int, int],
    negative_prompt: str | None = None,
) -> bytes:
    """通义万相（异步任务 + 轮询）生成背景图。"""
    import time

    api_key = settings.dashscope_key
    if not api_key:
        raise RuntimeError(
            "未配置 DASHSCOPE_API_KEY，无法调用通义万相出图（对话模型密钥不用于出图）"
        )
    auth = {"Authorization": f"Bearer {api_key}"}
    parameters: dict[str, Any] = {
        "size": _wanx_size(size[0], size[1]),
        "n": 1,
    }
    if negative_prompt:
        parameters["negative_prompt"] = negative_prompt
    resp = _http_client.post(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
        headers={**auth, "X-DashScope-Async": "enable"},
        json={
            "model": settings.image_model or "wanx2.1-t2i-turbo",
            "input": {"prompt": prompt},
            "parameters": parameters,
        },
        timeout=60,
    )
    resp.raise_for_status()
    task_id = resp.json()["output"]["task_id"]

    for _ in range(90):
        tr = _http_client.get(
            f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
            headers=auth,
            timeout=30,
        )
        tr.raise_for_status()
        out = tr.json().get("output", {})
        status = out.get("task_status")
        if status == "SUCCEEDED":
            url = out["results"][0]["url"]
            return _http_client.get(url, timeout=120).content
        if status in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"wanx 图像生成失败: {out}")
        time.sleep(2)
    raise TimeoutError("wanx 图像生成超时")


def edit_image(
    base_image_bytes: bytes,
    instruction: str,
    size_preset: str,
) -> bytes:
    """基于原图的图像编辑（图生图）：模特实拍服装等不宜抠图的商品，直接基于原图生成广告主视觉。

    通义万相 wanx2.1-imageedit 异步任务 + 轮询，原图以 base64 data URL 直传
    （不要求公网可访问，也不暴露 OSS 地址）。

    mock 模式返回渐变背景（保证全流程可演示/可测试）；调用失败抛异常，
    由调用方降级回"抠图 + 合成"路径，不阻断流水线。
    """
    import time

    if settings.mock_mode or settings.image_provider == "mock":
        return make_gradient_background(resolve_size(size_preset))

    if settings.image_provider != "dashscope":
        raise RuntimeError(f"图像编辑暂不支持 provider={settings.image_provider}（仅支持 dashscope/mock）")
    api_key = settings.dashscope_key
    if not api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用通义万相图像编辑")

    b64 = base64.b64encode(base_image_bytes).decode("ascii")
    auth = {"Authorization": f"Bearer {api_key}"}
    payload: dict[str, Any] = {
        "model": settings.image_edit_model or "wanx2.1-imageedit",
        "input": {
            "function": "description_edit",
            "prompt": instruction,
            "base_image_url": f"data:image/jpeg;base64,{b64}",
        },
        "parameters": {"n": 1},
    }

    resp = _http_client.post(
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis",
        headers={**auth, "X-DashScope-Async": "enable"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    task_id = resp.json()["output"]["task_id"]

    for _ in range(90):
        tr = _http_client.get(
            f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
            headers=auth,
            timeout=30,
        )
        tr.raise_for_status()
        out = tr.json().get("output", {})
        status = out.get("task_status")
        if status == "SUCCEEDED":
            url = out["results"][0]["url"]
            return _http_client.get(url, timeout=120).content
        if status in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"wanx 图像编辑失败: {out}")
        time.sleep(2)
    raise TimeoutError("wanx 图像编辑超时")


def _generate_http(
    prompt: str, size: tuple[int, int], negative_prompt: str | None = None
) -> bytes:
    """通用 HTTP 图像接口（Stable Diffusion WebUI / Flux API）。"""
    payload: dict[str, Any] = {"prompt": prompt, "width": size[0], "height": size[1]}
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    resp = _http_client.post(
        settings.llm_base_url or "http://localhost:7860/sdapi/v1/txt2img",
        json=payload,
        timeout=180,
    )
    resp.raise_for_status()
    data = resp.json()
    b64 = data["images"][0]
    return base64.b64decode(b64)
