#!/usr/bin/env bash
# 用 gh token 内联认证推送（绕过 Windows GCM 挂起问题）
# 用法: bash deploy/push-with-token.sh [branch]
set -uo pipefail

BRANCH="${1:-main}"
TOKEN="$(gh auth token 2>/dev/null)"
if [ -z "$TOKEN" ]; then
  echo "[x] 无法获取 gh token" >&2
  exit 1
fi

echo "[+] token 前缀: ${TOKEN:0:4}..., 长度: ${#TOKEN}"
echo "[+] scopes: $(gh auth status 2>&1 | grep -i scopes)"

AUTH_HEADER="Authorization: Basic $(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)"

echo "[+] 推送 $BRANCH ..."
if git -c credential.helper= \
       -c "http.https://github.com/.extraheader=$AUTH_HEADER" \
       push origin "$BRANCH" 2>&1; then
  echo "[+] 推送成功"
  git ls-remote origin "refs/heads/$BRANCH" 2>&1 | head -1
else
  rc=$?
  echo "[x] 推送失败 (rc=$rc)"
  echo "    若提示缺少 'workflow' scope，请先执行: gh auth refresh -h github.com -s workflow"
  exit $rc
fi
