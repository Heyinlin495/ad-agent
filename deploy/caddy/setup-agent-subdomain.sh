#!/usr/bin/env bash
# ============================================================
# agent.heyinlin.asia —— Caddy 子域接入 + TLS 修复脚本
#
# 背景（实测）：
#   服务器 80/443 由 **Caddy** 提供（HTTP 响应头 Server: Caddy），
#   不是 nginx。根域 heyinlin.asia / www 与旧 Streamlit 项目同走此 Caddy。
#   现象：http://agent.heyinlin.asia 返回 308 -> https，但 https 握手报
#         "TLSV1_ALERT_INTERNAL_ERROR"，即 Caddy 没有 agent 子域的证书。
#   原因：Caddy 配置里没有为 agent.heyinlin.asia 显式声明站点，
#         因此它不会为该 SNI 去申请证书。
#
# 本脚本做的事（幂等、安全、不碰根域既有站点块）：
#   1. 探测 Caddy 形态（systemd 二进制 / docker 容器）
#   2. 备份现有 Caddyfile
#   3. 若 Caddyfile 已 import /etc/caddy/conf.d/ -> 写 drop-in 片段
#      否则 -> 在 Caddyfile 末尾**追加**一个 agent 站点块（不动其它内容）
#   4. caddy validate + reload（失败则回滚备份）
#   5. 打印验证提示
#
# 用法（在服务器上以 root / sudo 执行）：
#   sudo bash setup-agent-subdomain.sh
# ============================================================
set -uo pipefail

DOMAIN="${DOMAIN:-agent.heyinlin.asia}"
UPSTREAM="${UPSTREAM:-127.0.0.1:8502}"     # web 容器发布的宿主端口
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
SNIPPET_DIR="/etc/caddy/conf.d"
STAMP="$(date +%Y%m%d-%H%M%S)"

log()  { printf '\033[1;34m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; }

# ---------- 1. 探测 Caddy ----------
CADDY_BIN=""
for b in "$(command -v caddy 2>/dev/null)" /usr/bin/caddy /usr/local/bin/caddy; do
  [ -n "$b" ] && [ -x "$b" ] && { CADDY_BIN="$b"; break; }
done
CADDY_CTR="$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -i caddy | head -1 | awk '{print $1}')"

log "caddy 二进制: ${CADDY_BIN:-未找到}"
log "caddy 容器:   ${CADDY_CTR:-无}"
log "Caddyfile:    ${CADDYFILE}"

if [ -z "$CADDY_BIN" ] && [ -z "$CADDY_CTR" ]; then
  err "既没有 caddy 二进制也没有 caddy 容器，无法自动处理。"
  err "请确认 Caddy 的安装方式后手动接入 ${DOMAIN} -> ${UPSTREAM}。"
  exit 1
fi

# ---------- 容器形态：给出明确指引（不改容器内文件，避免误伤）----------
if [ -n "$CADDY_CTR" ] && [ ! -f "$CADDYFILE" ]; then
  warn "Caddy 以容器运行，Caddyfile 在容器内（或挂载卷内）。"
  warn "请执行以下命令查看挂载与配置，然后手动加入站点块："
  echo
  echo "  docker inspect $CADDY_CTR --format '{{json .Mounts}}' | jq ."
  echo "  docker exec $CADDY_CTR cat /etc/caddy/Caddyfile"
  echo
  warn "把下面这段加入其 Caddyfile 后重启容器："
  echo
  printf '  %s {\n\tencode gzip\n\treverse_proxy %s\n  }\n' "$DOMAIN" "$UPSTREAM"
  echo
  echo "  docker restart $CADDY_CTR"
  exit 0
fi

[ -f "$CADDYFILE" ] || { err "未找到 $CADDYFILE"; exit 1; }

# ---------- 2. 备份 ----------
cp -a "$CADDYFILE" "${CADDYFILE}.bak-${STAMP}"
log "已备份: ${CADDYFILE}.bak-${STAMP}"

SNIPPET_BODY="${DOMAIN} {
	encode gzip
	reverse_proxy ${UPSTREAM}
}"

# ---------- 3. 写入（优先 drop-in）----------
USE_SNIPPET=0
if grep -qE '^[[:space:]]*import[[:space:]]+/etc/caddy/conf\.d/' "$CADDYFILE"; then
  USE_SNIPPET=1
fi

if [ "$USE_SNIPPET" = "1" ]; then
  mkdir -p "$SNIPPET_DIR"
  printf '%s\n' "$SNIPPET_BODY" > "${SNIPPET_DIR}/${DOMAIN}.caddy"
  log "已写入 drop-in 片段: ${SNIPPET_DIR}/${DOMAIN}.caddy"
else
  if grep -qE "^[[:space:]]*${DOMAIN}[[:space:]]*\{" "$CADDYFILE"; then
    log "Caddyfile 已存在 ${DOMAIN} 站点块，跳过追加。"
  else
    warn "Caddyfile 未 import ${SNIPPET_DIR}，改为在文件末尾追加站点块。"
    {
      printf '\n# ---- 由 setup-agent-subdomain.sh 追加 (%s) ----\n' "$STAMP"
      printf '%s\n' "$SNIPPET_BODY"
    } >> "$CADDYFILE"
    log "已追加 ${DOMAIN} 站点块到 $CADDYFILE"
  fi
fi

# ---------- 4. validate + reload（失败回滚）----------
if [ -n "$CADDY_BIN" ]; then
  if "$CADDY_BIN" validate --config "$CADDYFILE" >/dev/null 2>&1; then
    log "caddy validate 通过"
    if systemctl reload caddy 2>/dev/null || "$CADDY_BIN" reload --config "$CADDYFILE" 2>/dev/null; then
      log "caddy 已重载"
    else
      warn "自动 reload 失败，请手动执行: systemctl reload caddy"
    fi
  else
    err "caddy validate 失败，已回滚 $CADDYFILE"
    cp -a "${CADDYFILE}.bak-${STAMP}" "$CADDYFILE"
    err "回滚完成。请人工检查配置。"
    exit 1
  fi
fi

# ---------- 5. 验证 ----------
echo
log "完成。验证（证书签发可能需要数十秒）："
echo "  curl -sI  http://${DOMAIN}/          # 期望 308 -> https"
echo "  curl -sI  https://${DOMAIN}/         # 期望 200（首次可能稍等证书签发）"
echo "  curl -s   https://${DOMAIN}/api/v1/health"
echo
warn "注意：Caddy 自动申请证书需满足——"
warn "  1) ${DOMAIN} 的 A/AAAA 记录指向本机公网 IP"
warn "  2) 80 与 443 端口对公网可达（Let's Encrypt 校验）"
