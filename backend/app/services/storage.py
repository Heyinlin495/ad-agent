"""文件 / 对象存储服务（本地 / S3 兼容 / MinIO / 阿里云 OSS）。
"""
from __future__ import annotations

import ipaddress
import shutil
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse

from loguru import logger

from app.core.config import settings


class StorageConfigError(ValueError):
    """存储配置非法（如选择了本项目未实现的存储类型）。"""


class UnsafeUrlError(ValueError):
    """资源地址不在许可范围（防 SSRF / 路径穿越）。"""


def _is_private_host(host: str) -> bool:
    """判断主机是否指向内网 / 环回 / 元数据地址（SSRF 高危目标）。

    仅对 **IP 字面量**做判定。域名不做 DNS 解析：
    - 白名单前缀（本 bucket 主机名 / 本站相对路径）已是主控制手段，
      命中白名单即说明是可信来源；
    - DNS 解析结果受部署环境影响（多层 DNS、代理、私有解析域都会返回
      看似"私网"的地址），据此拦截会误伤正常业务。
    """
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 域名：交由前缀白名单判断
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def assert_safe_remote_url(url: str, allowed_prefixes: tuple[str, ...]) -> None:
    """校验远程资源地址：必须命中已知前缀，且主机不能是内网地址。

    资源的 URL 来源并不完全可信（产品表里的 original_image_url 可由接口写入），
    若无校验，服务端会替调用方下载任意地址（云元数据 169.254.169.254、内网服务等），
    构成 SSRF。
    """
    if not url.startswith(allowed_prefixes):
        raise UnsafeUrlError(f"资源地址不在许可范围: {url[:100]}")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError(f"不支持的协议: {parsed.scheme}")
    if _is_private_host(parsed.hostname or ""):
        raise UnsafeUrlError(f"资源主机不可用（内网/环回地址）: {parsed.hostname}")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """把缓存文件写成「同目录临时文件 + 原子改名」，避免半截文件污染缓存。

    图片处理读到一半被异常中断时，直接 write_bytes 会留下截断文件，后续
    读取（判 .exists() 直接复用）会拿到坏文件。先写临时文件再 os.replace，
    保证磁盘上要么没有、要么是完整文件。
    """
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class BaseStorage(ABC):
    """存储统一接口。save_* 返回可访问 URL，absolute_path 返回本地可处理路径。"""

    @abstractmethod
    def save_bytes(self, data: bytes, filename: str | None = None) -> str:
        ...

    @abstractmethod
    def save_file(self, src_path: str | Path, filename: str | None = None) -> str:
        ...

    @abstractmethod
    def absolute_path(self, url: str) -> Path:
        ...


class LocalStorage(BaseStorage):
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(self, data: bytes, filename: str | None = None) -> str:
        ext = Path(filename).suffix if filename else ".bin"
        name = f"{uuid.uuid4().hex}{ext}"
        (self.base_dir / name).write_bytes(data)
        return f"/files/{name}"

    def save_file(self, src_path: str | Path, filename: str | None = None) -> str:
        src = Path(src_path)
        ext = Path(filename).suffix if filename else src.suffix
        name = f"{uuid.uuid4().hex}{ext}"
        shutil.copy2(src, self.base_dir / name)
        return f"/files/{name}"

    def absolute_path(self, url: str) -> Path:
        # 只接受本存储自身的 /files/<name> 相对地址；显式拒绝任何路径穿越。
        name = url.removeprefix("/files/")
        if not name or name != Path(name).name or "/" in name or "\\" in name or name.startswith("."):
            raise UnsafeUrlError(f"非法的本地资源路径: {url[:100]}")
        path = (self.base_dir / name).resolve()
        if path.parent != self.base_dir:
            raise UnsafeUrlError(f"资源路径越界: {url[:100]}")
        return path


class S3Storage(BaseStorage):
    """S3 兼容对象存储（AWS S3 / MinIO / 其它 S3 兼容服务）。

    使用轻量 minio SDK（同时兼容 AWS S3 与 S3 兼容存储）。
    与 OssStorage 同一契约：save_* 返回可访问 URL，absolute_path 下载到本地缓存。
    """

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        cache_dir: str,
        region: str = "",
        *,
        public_url: str = "",
    ) -> None:
        from minio import Minio  # 延迟导入

        # 统一补全协议：endpoint 不带协议时默认 https；内网 MinIO 常用 http，交由配置显式给定
        host = endpoint
        secure = True
        if endpoint.startswith("https://"):
            host = endpoint.removeprefix("https://")
            secure = True
        elif endpoint.startswith("http://"):
            host = endpoint.removeprefix("http://")
            secure = False
        # 去掉路径尾部（如 http://minio:9000 → minio:9000，host 不应带 /path）
        host = host.rstrip("/").split("/", 1)[0]

        self._bucket = bucket
        self._client = Minio(host, access_key=access_key, secret_key=secret_key, secure=secure, region=region or None)
        self._bucket = bucket
        # 用于生成公开下载 URL 的主机名（默认用 endpooint 本身）
        self._public_host = host
        self._secure = secure
        self._public_url_prefix = public_url or ""

        # 确保 bucket 存在
        if not self._client.bucket_exists(bucket):
            logger.info(f"S3 bucket 不存在，自动创建: {bucket}")
            self._client.make_bucket(bucket)

        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _upload(self, key: str, data: bytes) -> str:
        from io import BytesIO

        self._client.put_object(
            self._bucket, key, BytesIO(data), length=len(data),
        )
        return self._public_url(key)

    def _public_url(self, key: str) -> str:
        if self._public_url_prefix:
            return f"{self._public_url_prefix.rstrip('/')}/{key}"
        scheme = "https" if self._secure else "http"
        return f"{scheme}://{self._public_host}/{self._bucket}/{key}"

    def save_bytes(self, data: bytes, filename: str | None = None) -> str:
        ext = Path(filename).suffix if filename else ".bin"
        key = f"ad-agent/{uuid.uuid4().hex}{ext}"
        return self._upload(key, data)

    def save_file(self, src_path: str | Path, filename: str | None = None) -> str:
        src = Path(src_path)
        ext = Path(filename).suffix if filename else src.suffix
        key = f"ad-agent/{uuid.uuid4().hex}{ext}"
        self._client.fput_object(self._bucket, key, str(src))
        return self._public_url(key)

    def absolute_path(self, url: str) -> Path:
        """远程对象下载到本地缓存，供图像处理使用。

        只允许当前 bucket 的公开 URL 或本站 /files 相对地址（SSRF 防护），
        其余一律拒绝。
        """
        if url.startswith("/files/"):
            name = url.removeprefix("/files/")
            if not name or name != Path(name).name:
                raise UnsafeUrlError(f"非法的本地资源路径: {url[:100]}")
            cache_path = self.cache_dir / name
            if cache_path.exists():
                return cache_path
            raise UnsafeUrlError(f"本地资源不存在: {url[:100]}")

        base = self._public_url("")
        allowed = (base, "https://" + self._public_host + "/", "http://" + self._public_host + "/")
        assert_safe_remote_url(url, allowed)

        name = url.rsplit("/", 1)[-1] or uuid.uuid4().hex
        if name != Path(name).name:
            raise UnsafeUrlError(f"非法的资源文件名: {name[:100]}")
        cache_path = self.cache_dir / name
        if not cache_path.exists():
            # 从 URL 还原 object key
            parsed = urlparse(url)
            object_name = parsed.path.lstrip("/")
            if object_name.startswith(f"{self._bucket}/"):
                object_name = object_name[len(self._bucket) + 1 :]
            # 先下载到临时文件再原子改名，避免半截文件进缓存
            import os
            import tempfile

            fd, tmp = tempfile.mkstemp(dir=str(self.cache_dir), suffix=".part")
            os.close(fd)
            try:
                self._client.fget_object(self._bucket, object_name, tmp)
                os.replace(tmp, cache_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        return cache_path


class OssStorage(BaseStorage):
    """阿里云 OSS 存储（需 oss2；bucket 建议设为公共读以便直接访问图片）。"""

    # 上传重试次数：瞬时网络中断（如 WinError 10053 连接被中断）不应让整条生成流水线失败
    UPLOAD_ATTEMPTS = 3

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        cache_dir: str,
    ) -> None:
        import oss2  # 延迟导入

        # 统一补全 https://：oss2 在 endpoint 不带协议时会退回明文 HTTP 上传
        # （下载 URL 用的是 https，上传走 HTTP 既不利于传输安全，也更容易被中间设备中断）
        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"
        auth = oss2.Auth(access_key_id, access_key_secret)
        self.bucket = oss2.Bucket(auth, endpoint, bucket)
        self._bucket_name = bucket
        self._endpoint = endpoint.removeprefix("https://").removeprefix("http://")
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _retry_upload(self, action) -> None:
        """带重试的上传：oss2 自身不做重试，网络抖动会直接抛错。"""
        import time

        import oss2
        import requests

        transient = (oss2.exceptions.OssError, requests.RequestException, OSError)
        for attempt in range(1, self.UPLOAD_ATTEMPTS + 1):
            try:
                action()
                return
            except transient as exc:
                if attempt >= self.UPLOAD_ATTEMPTS:
                    raise
                logger.warning(
                    f"OSS 上传失败（第 {attempt}/{self.UPLOAD_ATTEMPTS} 次），"
                    f"稍后重试: {type(exc).__name__}: {str(exc)[:120]}"
                )
                time.sleep(0.5 * attempt)

    def _public_url(self, key: str) -> str:
        return f"https://{self._bucket_name}.{self._endpoint}/{key}"

    def save_bytes(self, data: bytes, filename: str | None = None) -> str:
        ext = Path(filename).suffix if filename else ".bin"
        key = f"ad-agent/{uuid.uuid4().hex}{ext}"
        self._retry_upload(lambda: self.bucket.put_object(key, data))
        return self._public_url(key)

    def save_file(self, src_path: str | Path, filename: str | None = None) -> str:
        src = Path(src_path)
        ext = Path(filename).suffix if filename else src.suffix
        key = f"ad-agent/{uuid.uuid4().hex}{ext}"
        self._retry_upload(lambda: self.bucket.put_object_from_file(key, str(src)))
        return self._public_url(key)

    def absolute_path(self, url: str) -> Path:
        """远程对象下载到本地缓存，供图像处理使用。

        下载前先做白名单 + 私网校验（SSRF）：只允许本 bucket 的公开地址或
        本站 /files 相对地址，其余一律拒绝。
        """
        import httpx

        allowed = (
            f"https://{self._bucket_name}.{self._endpoint}/",
            f"http://{self._bucket_name}.{self._endpoint}/",
            "/files/",
        )
        if url.startswith("/files/"):
            # 本站相对地址：交给本地缓存目录直接读取，无需出网
            name = url.removeprefix("/files/")
            if not name or name != Path(name).name:
                raise UnsafeUrlError(f"非法的本地资源路径: {url[:100]}")
            cache_path = self.cache_dir / name
            if cache_path.exists():
                return cache_path
            raise UnsafeUrlError(f"本地资源不存在: {url[:100]}")

        assert_safe_remote_url(url, allowed)

        name = url.rsplit("/", 1)[-1] or uuid.uuid4().hex
        if name != Path(name).name:
            raise UnsafeUrlError(f"非法的资源文件名: {name[:100]}")
        cache_path = self.cache_dir / name
        if not cache_path.exists():
            data = httpx.get(url, timeout=60).content
            _atomic_write_bytes(cache_path, data)
        return cache_path


def get_storage() -> BaseStorage:
    """按配置返回存储实例。

    - local: 本地磁盘
    - s3 / minio: S3 兼容对象存储（AWS S3 / MinIO / 其它 S3 兼容服务）
    - oss: 阿里云 OSS
    未知 / 未实现类型会抛出 StorageConfigError（不再静默降级本地，避免生产数据
    悄悄落到容器本地磁盘导致丢失）。
    """
    storage_type = settings.storage_type

    if storage_type in ("s3", "minio"):
        required = (
            settings.s3_endpoint,
            settings.s3_bucket,
            settings.s3_access_key,
            settings.s3_secret_key,
        )
        if not all(required):
            raise StorageConfigError(
                f"STORAGE_TYPE={storage_type} 但 S3_ENDPOINT/S3_BUCKET/S3_ACCESS_KEY/"
                "S3_SECRET_KEY 未配置完整"
            )
        try:
            return S3Storage(
                endpoint=settings.s3_endpoint,
                bucket=settings.s3_bucket,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                cache_dir=str(Path(settings.storage_dir) / "cache"),
                public_url=settings.s3_public_url,
            )
        except StorageConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{storage_type.upper()} 存储初始化失败: {exc}")
            raise

    if storage_type == "oss":
        required = (
            settings.oss_bucket,
            settings.oss_endpoint,
            settings.oss_access_key_id,
            settings.oss_access_key_secret,
        )
        if not all(required):
            logger.warning("OSS 配置不完整，降级为本地存储")
            return LocalStorage(settings.storage_dir)
        try:
            return OssStorage(
                endpoint=settings.oss_endpoint,
                bucket=settings.oss_bucket,
                access_key_id=settings.oss_access_key_id,
                access_key_secret=settings.oss_access_key_secret,
                cache_dir=str(Path(settings.storage_dir) / "cache"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"OSS 初始化失败，降级本地存储: {exc}")
            return LocalStorage(settings.storage_dir)

    if storage_type == "local":
        return LocalStorage(settings.storage_dir)

    raise StorageConfigError(
        f"不支持的 STORAGE_TYPE={storage_type!r}（可选: local / s3 / minio / oss）"
    )


storage = get_storage()
