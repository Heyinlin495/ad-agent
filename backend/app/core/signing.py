"""静态资源签名 URL（HMAC-SHA256）。

配置 `FILE_URL_SECRET` 后，后端在返回给客户端的 /files/ 图片地址上追加
`exp`（过期时间戳）与 `sig`（对 "路径|过期时间" 的 HMAC 签名），中间件校验
通过才允许访问，从而实现「临时授权、可过期、前端无需持有长期令牌」。

未配置 `FILE_URL_SECRET` 时本模块不做任何处理（保持原有放行行为）。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from app.core.config import settings


def signing_enabled() -> bool:
    """是否启用签名 URL。"""
    return bool(settings.file_url_secret)


def _signature(path: str, exp: int) -> str:
    """对 "路径|过期时间" 计算 HMAC-SHA256 十六进制签名。"""
    msg = f"{path}|{exp}".encode("utf-8")
    return hmac.new(settings.file_url_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def sign_file_url(url: str, ttl: int | None = None) -> str:
    """给 /files/ 地址追加 exp/sig 参数。

    非 /files/ 地址、空地址或未启用签名时原样返回，保证调用方可无脑包装。
    """
    if not url or not signing_enabled():
        return url
    parts = urlsplit(url)
    path = parts.path
    if not path.startswith("/files/"):
        return url
    exp = int(time.time()) + int(ttl if ttl is not None else settings.file_url_ttl_seconds)
    sig = _signature(path, exp)
    extra = urlencode({"exp": exp, "sig": sig})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def verify_file_request(path: str, exp: str | None, sig: str | None) -> bool:
    """校验签名与有效期。未启用签名时返回 True（由调用方决定是否另行校验）。"""
    if not signing_enabled():
        return True
    try:
        exp_i = int(exp) if exp is not None else 0
    except (TypeError, ValueError):
        return False
    if exp_i < int(time.time()):
        return False
    return hmac.compare_digest(_signature(path, exp_i), sig or "")
