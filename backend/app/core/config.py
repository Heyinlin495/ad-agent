"""应用配置中心。

所有可配置项通过环境变量 / .env 文件注入（pydantic-settings）。
密钥一律不写死在代码中。
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 常见占位/示例密钥，视为"未配置"
_PLACEHOLDER_KEYS = {
    "", "sk-xxx", "sk-xxxxxxxx", "changeme", "your-api-key", "your_api_key",
    "todo", "none", "null",
}


class Settings(BaseSettings):
    """全局配置。字段名与 .env 中的大写环境变量一一对应。"""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用基础 ----------
    app_name: str = "Cross-border E-commerce Ad Agent"
    app_version: str = "0.1.0"
    environment: Literal["development", "production", "test"] = "development"
    # debug / mock_mode 用 None 表示「未显式配置」：由 model_validator 按环境取安全默认
    # （production 关闭、非生产开启），避免生产误带开发期默认值（traceback 泄露等）。
    debug: bool | None = None
    api_prefix: str = "/api/v1"
    # 允许的跨域来源；生产环境务必配置具体域名白名单，如 ["https://ad.example.com"]
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    # 是否允许携带凭证（Cookie/Authorization）。注意：与通配符 "*" 互斥，
    # 浏览器会拒绝 "*" + credentials 的组合，此处会在解析时自动降级并告警。
    cors_allow_credentials: bool = False

    # ---------- 数据库 ----------
    database_url: str = "sqlite:///./data/ad_agent.db"

    # ---------- 文件 / 对象存储 ----------
    storage_type: Literal["local", "s3", "minio", "oss"] = "local"
    storage_dir: str = "./data/storage"
    assets_dir: str = "../assets"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    # 可选：S3/MinIO 对外公开访问的基础 URL（如经 CDN/网关代理后的域名）。
    # 留空时 save_* 返回的 URL 直接用 s3_endpoint + bucket 拼接；
    # 若桶为私有，请配置反向代理/预签名方案后在此提供公开前缀。
    s3_public_url: str = ""
    # 阿里云 OSS
    oss_endpoint: str = ""  # 例如 oss-cn-beijing.aliyuncs.com
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""

    # ---------- 多模态 / 文本 LLM ----------
    llm_provider: Literal["openai", "claude", "qwen", "gemini", "kimi", "mock"] = "mock"
    llm_model: str = "gpt-4o"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_api_path: str = "/v1/messages"  # claude 原生协议的消息端点路径
    # 部分推理型模型（kimi-k3 / kimi-k2.x 等）只接受 temperature=1，非 1 会返回 400；
    # 客户端会自动为这类模型省略该参数并在日志中提示一次
    llm_temperature: float = 0.7
    llm_timeout: int = 60
    # 客户端限流：每分钟最大请求数（0 = 不限流）。
    # 用于适配上游账号的 RPM 上限（如 Kimi 新账号组织级 RPM 仅 3），
    # 否则流水线连续调用会大量触发 429
    llm_max_rpm: int = 0
    # 思考模式开关："" 不干预 / "enabled" / "disabled"（仅 kimi-k2.x 等支持思考开关的模型生效）
    # 关闭思考可显著降低延迟与输出 token（推理 token 按输出计费）
    llm_thinking: Literal["", "enabled", "disabled"] = ""
    # 推理强度（仅 kimi-k3 生效）："low" / "high" / "max"；留空用上游默认（max）
    llm_reasoning_effort: str = ""
    vision_provider: str = "mock"
    vision_model: str = "gpt-4o"

    # ---------- 阿里云 DashScope（Embedding + 通义万相出图） ----------
    # 与对话模型解耦：切换到 Kimi 等第三方模型后，RAG 向量检索与出图仍走 DashScope，
    # 因此需要独立密钥。留空时若 LLM_PROVIDER 仍为 qwen，则回退复用 LLM_API_KEY。
    dashscope_api_key: str = ""
    # Embedding 服务地址；留空时按嵌入提供商自动选择
    embedding_base_url: str = ""

    # ---------- Embedding ----------
    embedding_provider: Literal["openai", "bge-m3", "dashscope", "mock"] = "mock"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # ---------- 向量库 ----------
    vector_store_type: Literal["chroma", "milvus", "mock"] = "chroma"
    chroma_dir: str = "./data/chroma"
    milvus_uri: str = ""

    # ---------- Rerank ----------
    rerank_provider: Literal["bge", "none"] = "none"
    rerank_model: str = "BAAI/bge-reranker-base"
    rerank_top_n: int = 5

    # ---------- 图像生成 ----------
    image_provider: Literal["openai", "dashscope", "sd", "flux", "mock"] = "mock"
    image_model: str = ""
    # 图生图（图像编辑）模型：模特实拍服装等不宜抠图的商品，基于原图指令编辑生成广告主视觉
    image_edit_model: str = "wanx2.1-imageedit"

    # ---------- Mock 模式 ----------
    # None = 未显式配置：生产环境关闭、非生产开启（见 model_validator）
    mock_mode: bool | None = None  # 无 API Key 时启用，全流程可演示

    # ---------- 可观测 ----------
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langsmith_enabled: bool = False
    langsmith_api_key: str = ""

    # ---------- 安全 / 限流 ----------
    rate_limit_per_minute: int = 60
    max_upload_size_mb: int = 50
    # 广告生成任务并发上限（独立线程池，避免与 Web 请求线程互相挤占）
    task_max_workers: int = 4
    # 在途任务总上限（排队 + 执行中）：超过即拒绝提交，防止线程池待办队列无上限增长。
    # 0 表示按 task_max_workers 自动取值（见 task_runner）。
    task_max_inflight: int = 0
    # 非空时 /files 静态资源需携带 token（?token= 或 X-File-Token 头）才能访问；
    # 本地开发留空即不校验，生产部署请务必配置
    file_access_token: str = ""
    # 静态资源签名 URL 密钥（HMAC）。非空时后端返回的图片地址会自动带 exp/sig 签名，
    # /files 校验签名通过才放行（比固定 token 更安全：可过期、不暴露长期令牌）。
    file_url_secret: str = ""
    file_url_ttl_seconds: int = 86400  # 签名有效期（秒），默认 1 天

    # ---------- 迁移 ----------
    # 启动时自动执行 Alembic 升级到最新版本；失败则回退到轻量建表/补列兜底。
    auto_migrate: bool = True

    # ---------- 安全 ----------
    # 生产环境存在致命安全问题时是否直接拒绝启动（False 则仅告警）
    strict_security: bool = True

    # ---------- 成本 ----------
    # 每百万 token 单价（USD），用于把 usage_logs.cost 算出真实金额。
    # 形如 {"qwen-flash": [0.05, 0.4]}（[输入, 输出]），未命中的模型按 0 计。
    model_pricing: dict[str, list[float]] = Field(default_factory=dict)

    # ---------- 指标 ----------
    # /metrics 端点访问令牌；非空时需携带 ?token= 或 X-Metrics-Token 头
    metrics_token: str = ""

    @model_validator(mode="after")
    def _apply_env_safe_defaults(self) -> "Settings":
        """按环境为非显式配置项（None）取安全默认值。

        - debug：生产默认 False，非生产默认 True
        - mock_mode：生产默认 False，非生产默认 True
        这样即便部署者漏配，production 也不会带着调试/模拟开关上线。
        显式配置（True/False）始终优先。
        """
        production = self.environment == "production"
        if self.debug is None:
            self.debug = not production
        if self.mock_mode is None:
            self.mock_mode = not production
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def dashscope_key(self) -> str:
        """DashScope 密钥（Embedding / 通义万相出图用）。

        优先取 DASHSCOPE_API_KEY；未配置且对话模型仍是 qwen 时回退复用 LLM_API_KEY，
        保证老部署平滑迁移；已切到第三方模型（如 kimi）且未单独配置时返回空，
        由调用方降级处理，避免误用第三方密钥去请求阿里云接口。
        """
        key = (self.dashscope_api_key or "").strip()
        if key:
            return key
        if self.llm_provider == "qwen":
            return (self.llm_api_key or "").strip()
        return ""

    @property
    def cors_resolved(self) -> tuple[list[str], bool]:
        """解析 CORS 配置：通配符 "*" 与 allow_credentials 不能共存。

        返回 (origins, allow_credentials)。当 origins 含 "*" 且要求携带凭证时，
        自动把 allow_credentials 降级为 False（否则浏览器会直接拒绝所有带凭证请求）。
        """
        origins = list(self.cors_origins or [])
        allow_credentials = self.cors_allow_credentials
        if "*" in origins and allow_credentials:
            allow_credentials = False
        return origins, allow_credentials

    def security_issues(self) -> list[str]:
        """启动期安全自检，返回问题描述列表（空列表表示未发现问题）。"""
        issues: list[str] = []
        if "*" in self.cors_origins:
            issues.append(
                "CORS 使用通配符 '*'（CORS_ORIGINS），生产环境应配置具体域名白名单"
            )
        if not self.file_access_token and not self.file_url_secret:
            issues.append(
                "未配置 FILE_ACCESS_TOKEN / FILE_URL_SECRET，"
                "/files 下上传原图与生成图可被任意人访问"
            )
        if self.llm_provider != "mock" and (
            (self.llm_api_key or "").strip().lower() in _PLACEHOLDER_KEYS
        ):
            issues.append(
                f"已启用真实 LLM（LLM_PROVIDER={self.llm_provider}）但 LLM_API_KEY 未配置"
            )
        if self.debug:
            issues.append("DEBUG 仍为 True，生产环境请设置 DEBUG=false")
        if self.embedding_provider == "dashscope" and not self.dashscope_key:
            issues.append(
                "EMBEDDING_PROVIDER=dashscope 但未配置 DASHSCOPE_API_KEY，"
                "RAG 向量检索将降级为本地 mock"
            )
        if self.image_provider == "dashscope" and not self.dashscope_key:
            issues.append(
                "IMAGE_PROVIDER=dashscope 但未配置 DASHSCOPE_API_KEY，"
                "通义万相出图将失败并回退纯色背景"
            )
        if self.mock_mode:
            issues.append("MOCK_MODE 仍为 True，将不会调用真实模型")
        if self.storage_type == "local" and self.is_production:
            issues.append(
                "生产环境使用本地文件存储（STORAGE_TYPE=local），建议改用对象存储"
            )
        return issues


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例（带缓存）。"""
    return Settings()


settings = get_settings()
