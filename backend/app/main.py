"""FastAPI 应用入口。"""
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import init_db
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging


def _forbidden_payload() -> dict:
    """统一的 403 响应体（与 exceptions.py 的 {code, message, data} 信封一致）。"""
    return {"code": 40300, "message": "Forbidden", "data": None}


def normalize_request_path(raw_path: str) -> tuple[str, bool]:
    """把请求路径做百分号解码 + 规范化，返回 (规范化路径, 是否含穿越)。

    直接用原始 path 判断 /files/ 前缀会漏掉 /files/../files/x、%2e%2e 等形态，
    使授权校验依赖框架的二次解析——一旦两者解析结果不同即可绕过。
    """
    decoded = unquote(raw_path)
    parts = PurePosixPath(decoded).parts
    if ".." in parts:
        return decoded, True
    norm = "/" + "/".join(p for p in parts if p not in ("/", "."))
    return norm, False


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # 启动期安全自检：生产环境不安全配置将拒绝启动（见 core/security.py）
    from app.core.security import run_security_audit

    run_security_audit()
    init_db()
    # 确保存储目录存在
    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    # 灌入内置示例知识库（仅当知识库为空），并在向量库为空时从数据库重建索引
    from app.core.database import SessionLocal
    from app.rag.vector_store import get_vector_store
    from app.services.kb_service import rebuild_index, seed_knowledge_base
    from app.services.task_service import recover_orphan_tasks

    db = SessionLocal()
    try:
        seed_knowledge_base(db)
        if get_vector_store().count() == 0:
            rebuild_index(db)
        # 上次进程残留的 running/pending 任务本进程无法续跑，标记为失败避免永久卡住
        recover_orphan_tasks(db)
    finally:
        db.close()

    # 后台预热抠图模型（不阻塞启动）：受限机器上 ONNX 会话首次初始化可达分钟级，
    # 放到启动期完成，避免部署/重启后的第一个识别请求独自承担并超时。
    import threading

    from app.image.preprocess import warmup_rembg

    threading.Thread(target=warmup_rembg, name="rembg-warmup", daemon=True).start()

    yield
    # 退出：等待在途任务收尾（不阻塞过久）
    from app.services import task_runner

    task_runner.shutdown(wait=False)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# 中间件
_cors_origins, _cors_credentials = settings.cors_resolved
if "*" in _cors_origins and settings.cors_allow_credentials:
    logger.warning(
        "CORS 配置冲突：allow_origins 含通配符 '*' 时不允许携带凭证，"
        "已自动将 allow_credentials 降级为 False。请在 CORS_ORIGINS 配置具体域名。"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# 静态资源访问控制：配置了 FILE_ACCESS_TOKEN（固定令牌）或 FILE_URL_SECRET（签名 URL）
# 时启用校验；两者都未配置则放行（本地开发默认）。生产部署请至少配置其一。
if settings.file_access_token or settings.file_url_secret:

    @app.middleware("http")
    async def _file_access_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        norm_path, has_traversal = normalize_request_path(request.url.path)
        if has_traversal:
            return JSONResponse(status_code=403, content=_forbidden_payload())
        if norm_path.startswith("/files/"):
            from app.core.signing import verify_file_request

            token = request.query_params.get("token") or request.headers.get(
                "x-file-token", ""
            )
            token_ok = bool(settings.file_access_token) and token == settings.file_access_token
            # 签名必须基于**规范化后**的路径，保证校验对象与实际访问资源一致
            sign_ok = bool(settings.file_url_secret) and verify_file_request(
                norm_path,
                request.query_params.get("exp"),
                request.query_params.get("sig"),
            )
            if not (token_ok or sign_ok):
                return JSONResponse(status_code=403, content=_forbidden_payload())
        return await call_next(request)

# 全局异常处理
register_exception_handlers(app)

# 静态文件（图片 / 附件）
Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=settings.storage_dir), name="files")

# 路由
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"app": settings.app_name, "docs": "/docs", "api": settings.api_prefix}
