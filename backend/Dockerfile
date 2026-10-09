# syntax=docker/dockerfile:1.7
# ============================================================
# 跨境电商 AI 广告生成 Agent —— 后端镜像
#
# 设计目标：镜像尽量小 · 构建尽量快 · 默认非 root 运行（安全）
#
# 依赖策略（BUILD_DEPS）：
#   默认 core  —— 只装 requirements-core.txt（无 torch/chromadb/rembg 等重型栈，
#                        镜像约 350MB，构建快；应用在 Mock 模式/纯 Pillow 合成下可跑）。
#   传 full     真正启用 RAG 向量检索 / CrossEncoder / rembg 抠图 / MinIO/OSS 对象存储等，
#                        镜像约 2GB+。需要时构建：--build-arg BUILD_DEPS=full
#   docker build --build-arg BUILD_DEPS=full -t ad-agent-backend:full ./backend
# ============================================================

# ---------- Stage 1: builder ----------
# 用 3.12：项目依赖按 Python 3.12 锁定（rembg==2.0.59 等无 3.13 wheel）
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

ARG BUILD_DEPS=core
# 先把依赖清单拷进来（利用层缓存：依赖清单不变就跳过整层 pip）
COPY requirements.txt requirements-core.txt ./
# full => 安装完整依赖；其余（core）=> 仅安装核心依赖
RUN echo "---> pip 安装 [$BUILD_DEPS] 依赖" \
  && python -m venv /opt/venv \
  && . /opt/venv/bin/activate \
  && python -m pip install --upgrade pip \
  && if [ "$BUILD_DEPS" = "full" ]; then \
       pip install -r requirements.txt; \
     else \
       pip install -r requirements-core.txt; \
     fi

# ---------- Stage 2: 运行时（精简、非 root）----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    ASSETS_DIR=/app/assets

ARG BUILD_DEPS=core

# 系统运行库：仅 full 需要 cv2/rembg/onnx 的系统库；core 不需要，保持镜像最小
RUN if [ "$BUILD_DEPS" = "full" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
           libgl1 libglib2.0-0 libgomp1 \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# 只拷贝已安装好的依赖与产物，不重复 pip 环节（多阶段关键收益）
COPY --from=builder /opt/venv /opt/venv
COPY . /app

WORKDIR /app

# 创建非 root 运行用户，并给可写目录授权
# 固定 uid/gid=1000：便于 compose 挂载的 named volume（/data）在部署时用 chown 1000:1000 修正属主，
# 否则卷默认 root 属主，非 root 的 app 用户无法写 /data/storage（会导致 storage 初始化 PermissionError）
RUN groupadd -r -g 1000 app && useradd -r -u 1000 -g 1000 -d /app app \
    && mkdir -p /app/data /app/assets /data/storage \
    && chown -R app:app /app /data

# 非 root 运行（安全）：镜像内以 app 用户启动；
# read_only 根文件系统 / 弱化 capabilities / no-new-privileges 等在
# docker-compose.yml 或 K8s 运行时层设置（见 compose 的 security_opt / init）。
USER app
EXPOSE 8000

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4); sys.exit(0)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]