FROM nginx:1.27-alpine

# 静态资源
COPY index.html /usr/share/nginx/html/index.html
COPY css/ /usr/share/nginx/html/css/
COPY js/ /usr/share/nginx/html/js/

# 站点配置（含 /api、/files 反向代理与 SSE 配置）
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
