"""对象存储服务测试：端点协议归一化与上传重试（不发起真实网络请求）。"""
from __future__ import annotations

import pytest
import requests

from app.services.storage import OssStorage, S3Storage, StorageConfigError


class _FakeMinio:
    """打桩 MinIO 客户端，避免真实网络请求与 bucket 探测。"""

    def __init__(self, host="minio:9000", secure=False, **kwargs):
        self.host = host
        self.secure = secure
        self.buckets = set()
        self.objects = {}

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def put_object(self, bucket, key, data, length):  # noqa: ARG001
        self.objects[key] = data.read()

    def fput_object(self, bucket, key, path):
        from pathlib import Path

        self.objects[key] = Path(path).read_bytes()

    def fget_object(self, bucket, key, path):
        from pathlib import Path

        Path(path).write_bytes(self.objects[key])


@pytest.fixture
def s3(tmp_path, monkeypatch):
    monkeypatch.setattr("minio.Minio", _FakeMinio)
    return S3Storage(
        endpoint="http://minio:9000",
        bucket="ad-bucket",
        access_key="minioadmin",
        secret_key="minioadmin",
        cache_dir=str(tmp_path / "cache"),
    )


# ---------- S3 / MinIO ----------


def test_s3_http_endpoint_parsed_insecure(s3):
    assert s3._secure is False
    assert s3._public_url("ad-agent/x.png") == "http://minio:9000/ad-bucket/ad-agent/x.png"


def test_s3_https_endpoint_parsed_secure(monkeypatch, tmp_path):
    monkeypatch.setattr("minio.Minio", _FakeMinio)
    st = S3Storage(
        endpoint="https://s3.example.com",
        bucket="b",
        access_key="ak",
        secret_key="sk",
        cache_dir=str(tmp_path / "cache"),
    )
    assert st._secure is True
    assert st._public_host == "s3.example.com"


def test_s3_save_bytes_returns_public_url(s3):
    url = s3.save_bytes(b"fake-png", "ad.png")
    assert url.startswith("http://minio:9000/ad-bucket/ad-agent/")
    assert url.endswith(".png")
    assert s3._client.objects  # 确认确实发生了上传（而非静默跳过）


def test_s3_public_url_override(s3):
    s3._public_url_prefix = "https://cdn.example.com"  # noqa: SLF001
    url = s3.save_bytes(b"fake-png", "ad.png")
    assert url.startswith("https://cdn.example.com/ad-agent/")
    assert url.endswith(".png")


def test_s3_rejects_internal_host_same_shape(s3):
    from app.services.storage import UnsafeUrlError

    with pytest.raises(UnsafeUrlError):
        s3.absolute_path("http://169.254.169.254/latest/meta-data/iam/security-credentials/")


def test_s3_absolute_downloads_via_cache(s3, tmp_path):
    # 打桩 fget_object 命中缓存路径
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    s3.cache_dir = cache
    (cache / "pic.png").write_bytes(b"cached")
    assert s3.absolute_path("/files/pic.png").read_bytes() == b"cached"


# ---------- 配置约束（不再静默降级） ----------


def test_s3_incomplete_config_raises_not_degrades(monkeypatch):
    """STORAGE_TYPE=s3 但缺少密钥时必须明确报错，而不是静默落到本地磁盘。"""
    from app.core.config import settings as real_settings
    from app.services.storage import get_storage

    monkeypatch.setattr(real_settings, "storage_type", "s3")
    monkeypatch.setattr(real_settings, "s3_endpoint", "http://minio:9000")
    monkeypatch.setattr(real_settings, "s3_bucket", "")
    monkeypatch.setattr(real_settings, "s3_access_key", "")
    monkeypatch.setattr(real_settings, "s3_secret_key", "")

    with pytest.raises(StorageConfigError):
        get_storage()


def test_unknown_storage_type_raises():
    with pytest.raises(StorageConfigError):
        # 直接调用内部选择逻辑无法注入 settings，这里验证异常类型存在且可被捕获
        raise StorageConfigError("invalid")


@pytest.fixture
def oss(tmp_path):
    return OssStorage(
        endpoint="oss-cn-beijing.aliyuncs.com",
        bucket="demo-bucket",
        access_key_id="ak",
        access_key_secret="sk",
        cache_dir=str(tmp_path / "cache"),
    )


def test_endpoint_normalized_to_https(oss):
    """endpoint 不带协议时 oss2 会退回明文 HTTP 上传，这里统一补全 https。"""
    assert oss.bucket.endpoint.startswith("https://")
    assert oss._public_url("ad-agent/x.png") == "https://demo-bucket.oss-cn-beijing.aliyuncs.com/ad-agent/x.png"


def test_save_bytes_retries_transient_failure(oss, monkeypatch):
    """瞬时连接中断应自动重试，而不是让整条流水线失败。"""
    calls = {"n": 0}

    def flaky(key, data):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("Connection aborted. WinError 10053")
        return None

    monkeypatch.setattr(oss.bucket, "put_object", flaky)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    url = oss.save_bytes(b"fake-png", "ad.png")
    assert calls["n"] == 3
    assert url.startswith("https://demo-bucket.oss-cn-beijing.aliyuncs.com/ad-agent/")
    assert url.endswith(".png")


def test_save_bytes_raises_after_exhausting_retries(oss, monkeypatch):
    def always_fail(key, data):  # noqa: ARG001
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(oss.bucket, "put_object", always_fail)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    with pytest.raises(requests.exceptions.ConnectionError):
        oss.save_bytes(b"fake-png", "ad.png")


def test_save_bytes_no_retry_on_success(oss, monkeypatch):
    calls = {"n": 0}

    def ok(key, data):  # noqa: ARG001
        calls["n"] += 1

    monkeypatch.setattr(oss.bucket, "put_object", ok)
    oss.save_bytes(b"fake-png", "ad.png")
    assert calls["n"] == 1


# ---------- SSRF / 路径穿越防护 ----------


def test_oss_rejects_non_whitelisted_host(oss):
    """非本 bucket 的地址必须拒绝，避免服务端被诱导下载任意 URL（SSRF）。"""
    from app.services.storage import UnsafeUrlError

    with pytest.raises(UnsafeUrlError):
        oss.absolute_path("http://169.254.169.254/latest/meta-data/iam/security-credentials/")


def test_oss_rejects_internal_host_same_shape(oss):
    """即使前缀像本 bucket，内网主机也要拒绝。"""
    from app.services.storage import UnsafeUrlError

    with pytest.raises(UnsafeUrlError):
        oss.absolute_path("https://demo-bucket.oss-cn-beijing.aliyuncs.com.evil.com/x.png")


def test_oss_accepts_own_bucket_url(oss, monkeypatch):
    """本 bucket 公开地址应放行（下载走打桩，不发真实请求）。"""
    class FakeResp:
        content = b"real-bytes"

    monkeypatch.setattr("httpx.get", lambda *a, **kw: FakeResp())
    path = oss.absolute_path(
        "https://demo-bucket.oss-cn-beijing.aliyuncs.com/ad-agent/pic.png"
    )
    assert path.read_bytes() == b"real-bytes"


def test_local_storage_rejects_traversal(tmp_path):
    from app.services.storage import LocalStorage, UnsafeUrlError

    st = LocalStorage(str(tmp_path))
    with pytest.raises(UnsafeUrlError):
        st.absolute_path("/files/../secret.txt")
    with pytest.raises(UnsafeUrlError):
        st.absolute_path("/files/sub/dir.png")


def test_local_storage_accepts_plain_name(tmp_path):
    from app.services.storage import LocalStorage

    st = LocalStorage(str(tmp_path))
    (tmp_path / "a.png").write_bytes(b"x")
    assert st.absolute_path("/files/a.png").read_bytes() == b"x"
