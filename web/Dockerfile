# syntax=docker/dockerfile:1.7
# ============================================================
# Web 前端 —— 零构建原生 ESM 静态站点，nginx 托管 + 反代后端
#
# 保持 80 端口是因为 nginx.conf 与 compose 对外映射 HOST:8502->80 已约定；
# nginx:alpine 默认 master 需以 root 绑定 80（workers 已降权为 nginx 用户）。
# 如需彻底非 root，可改用 nginxinc/nginx-unprivileged 并把监听改 8080。
# ============================================================
FROM nginx:1.27-alpine

# 仅保留必要的静态产物（零构建工程，无编译步骤，构建极快）
COPY index.html /usr/share/nginx/html/index.html
COPY css/ /usr/share/nginx/html/css/
COPY js/ /usr/share/nginx/html/js/

# 站点配置：含 /api、/files 反代、gzip、安全响应头、静态缓存策略
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
STOPSIGNAL SIGQUIT

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1